import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import job_logs
from app.core.database import get_db
from app.services import (
    glossary_service,
    history_service,
    srt_service,
)

router = APIRouter()

# Un SRT de largometraje ronda los 100 KB; 5 MB ya es anómalo. Sin tope,
# un archivo gigante agotaba RAM y crédito OpenAI sin freno (SEC-02).
_MAX_SRT_BYTES = 5 * 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════
# ROOT
# ══════════════════════════════════════════════════════════════════════════
@router.get("/")
def root():
    from app.core.config import APP_VERSION
    return {"message": "API lista para traducir con GPT-4o 🤖", "version": APP_VERSION}


# ══════════════════════════════════════════════════════════════════════════
# TRANSLATE — patrón 202+polling (v3.8): valida, encola y responde en <1 s.
# El trabajo real lo hace el worker del backend (app/services/job_runner.py);
# la UI sigue el progreso con GET /jobs/by-uuids + logs y descarga el
# resultado de GET /jobs/{id}/download cuando status=completed.
# ══════════════════════════════════════════════════════════════════════════
@router.post("/translate", status_code=202)
def translate_subtitle(
    # `def` a propósito: FastAPI lo ejecuta en el threadpool, así el
    # write_text + parse + commit no tocan el event loop del polling.
    file: UploadFile = File(...),
    context: str = Form(""),
    target_lang: str = Form("es"),
    cpl: int = Form(38),
    auto_context: bool = Form(False),
    job_uuid: str = Form(""),  # uuid del frontend para logs en vivo (opcional)
    db: Session = Depends(get_db),
):
    """Valida el .srt, lo guarda en data/uploads y crea un Job 'queued'."""
    import uuid as _uuid

    if not file.filename.endswith(".srt"):
        raise HTTPException(status_code=400, detail="Sube un archivo .srt válido.")
    if not (30 <= cpl <= 50):
        raise HTTPException(status_code=400, detail="cpl fuera de rango (30-50).")

    content = file.file.read()
    if len(content) > _MAX_SRT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="SRT demasiado grande (máx 5 MB). Un largometraje ronda los 100 KB.",
        )

    # Validar ANTES de encolar: un SRT corrupto debe fallar aquí con un
    # mensaje claro, no minutos después dentro del worker.
    try:
        text_content = content.decode("utf-8-sig")  # tolera BOM
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="El SRT no está en UTF-8. Guárdalo como UTF-8 y vuelve a subirlo.",
        )
    try:
        # Normalizado: índices None o duplicados colapsaban el dict del
        # pipeline (traducciones cruzadas) o reventaban al persistir.
        subtitulos = srt_service.parse_srt_normalizado(text_content)
    except Exception:
        raise HTTPException(status_code=400, detail="El archivo no es un SRT válido.")
    n_cues = len(subtitulos)
    if n_cues == 0:
        raise HTTPException(status_code=400, detail="El SRT no contiene subtítulos.")

    job_uuid = (job_uuid or "").strip() or _uuid.uuid4().hex[:12]

    # Un uuid con job vivo no puede reutilizarse: dos jobs compartirían
    # el mismo archivo de uploads y la cancelación sería ambigua.
    existente = history_service.get_job_by_uuid(db, job_uuid)
    if existente is not None and existente.status in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Ya hay un job activo con ese uuid (id={existente.id}).",
        )

    from app.services.job_runner import upload_path
    try:
        upload_path(job_uuid).write_text(
            srt_service.compose_srt(subtitulos), encoding="utf-8",
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el SRT: {e}")

    job = history_service.create_queued_job(
        db,
        filename=file.filename,
        target_lang=target_lang,
        cpl=cpl,
        context=context,
        job_uuid=job_uuid,
        auto_context=auto_context,
        n_cues=n_cues,
    )
    posicion = len(history_service.list_active_jobs(db))
    job_logs.log(
        job_uuid,
        f"📥 Encolado · {len(content) / 1024:.1f} KB · {n_cues} cues · "
        f"cpl={cpl} · {target_lang} · posición {posicion}",
    )

    return {
        "job_id":   job.id,
        "job_uuid": job_uuid,
        "status":   "queued",
        "position": posicion,
    }


# ══════════════════════════════════════════════════════════════════════════
# GLOSSARY  (CRUD)
# ══════════════════════════════════════════════════════════════════════════
class GlossaryTermIn(BaseModel):
    source:   str = Field(..., min_length=1, max_length=256)
    target:   str = Field(..., min_length=1, max_length=256)
    category: str = Field("término",  max_length=64)
    note:     str = Field("",         max_length=2000)


@router.get("/glossary")
def list_glossary(db: Session = Depends(get_db)):
    return [t.to_dict() for t in glossary_service.list_terms(db)]


@router.post("/glossary", status_code=201)
def add_glossary_term(payload: GlossaryTermIn, db: Session = Depends(get_db)):
    try:
        term = glossary_service.add_term(
            db,
            source=payload.source,
            target=payload.target,
            category=payload.category,
            note=payload.note,
        )
        return term.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/glossary/{term_id}", status_code=204)
def delete_glossary_term(term_id: int, db: Session = Depends(get_db)):
    if not glossary_service.delete_term(db, term_id):
        raise HTTPException(status_code=404, detail="Término no encontrado.")
    return Response(status_code=204)


@router.get("/glossary/export.csv")
def export_glossary_csv(db: Session = Depends(get_db)):
    """Devuelve el glosario como CSV listo para Excel español:
       - UTF-8 con BOM (Excel detecta encoding y respeta tildes/ñ)
       - Separador ';' (default de Excel en locale ES)
       - QUOTE_MINIMAL: solo se citan campos con caracteres especiales.
    """
    terms = glossary_service.list_terms(db)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["source", "target", "category", "note"],
        delimiter=";",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for t in terms:
        writer.writerow({
            "source":   t.source,
            "target":   t.target,
            "category": t.category,
            "note":     t.note,
        })
    # BOM (﻿) al inicio para que Excel detecte UTF-8 correctamente
    csv_bytes = ("﻿" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="glossary.csv"'},
    )


@router.post("/glossary/import")
async def import_glossary_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Importa un CSV con columnas source,target,category,note.

    Auto-detecta:
      - Encoding: UTF-8 (con o sin BOM) o Windows-1252 (típico de Excel ES
        cuando se guarda como CSV antiguo).
      - Delimiter: ';' (Excel ES), ',' (estándar internacional), tab.

    Idempotente por dedup case-insensitive de (source, target).
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Sube un archivo .csv válido.")

    content = await file.read()
    # Cascada de encodings probables
    text: str | None = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(status_code=400, detail="No se pudo decodificar el CSV.")

    # Auto-detectar el separador con csv.Sniffer
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        # Si Sniffer falla (CSV con una sola columna o muy raro),
        # caer al estándar internacional como fallback.
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="El CSV está vacío o sin header.")

    return glossary_service.import_csv_rows(db, rows)


# ══════════════════════════════════════════════════════════════════════════
# JOBS  (historial)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/jobs")
def list_jobs(limit: int = 50, db: Session = Depends(get_db)):
    return [j.to_dict() for j in history_service.list_jobs(db, limit=limit)]


# NOTA de orden: estas rutas de 2 segmentos deben declararse ANTES de
# /jobs/{job_id} — si no, "active" o "by-uuids" intentarían parsearse
# como int y devolverían 422.
@router.get("/jobs/active")
def list_active_jobs(db: Session = Depends(get_db)):
    """Jobs vivos (queued/running) de TODO el backend. La UI los pinta
    aunque la sesión del navegador sea nueva: la cola sobrevive a un F5."""
    return {"jobs": [j.to_dict() for j in history_service.list_active_jobs(db)]}


@router.get("/jobs/by-uuids")
def get_jobs_by_uuids(uuids: str = "", db: Session = Depends(get_db)):
    """Snapshot de varios jobs por uuid (separados por coma) en una sola
    query — es el polling de la cola de la UI, cada 2,5 s."""
    lista = [u.strip() for u in uuids.split(",") if u.strip()][:100]
    return {"jobs": [j.to_dict() for j in history_service.get_jobs_by_uuids(db, lista)]}


@router.post("/jobs/by-uuid/{job_uuid}/cancel", status_code=202)
def cancel_job_endpoint(job_uuid: str, db: Session = Depends(get_db)):
    """Cancela un job por uuid.

    - 'queued': se marca cancelled directamente (no llegará al worker).
    - 'running': se marca para cancelación cooperativa — el worker corta
      al terminar el chunk en curso (el request a OpenAI en vuelo no es
      interrumpible; ese coste ya está gastado).
    - terminal: no-op informativo.
    """
    from app.services.job_runner import mark_cancel

    job = history_service.get_job_by_uuid(db, job_uuid)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado.")

    if job.status == "queued":
        # Marcar también el registro: si el worker lo toma justo ahora
        # (carrera queued→running), verá la marca y lo descartará.
        mark_cancel(job_uuid)
        history_service.mark_cancelled(db, job)
        job_logs.log(job_uuid, "✖ Quitado de la cola antes de empezar", level="warn")
        return {"status": "cancelled", "was": "queued"}
    if job.status == "running":
        mark_cancel(job_uuid)
        job_logs.log(job_uuid, "✖ Cancelación solicitada — se corta al final del chunk en curso…", level="warn")
        return {"status": "cancelling", "was": "running"}
    return {"status": job.status, "was": job.status}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = history_service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    return {
        **job.to_dict(),
        "translations": [t.to_dict() for t in job.translations],
    }


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, db: Session = Depends(get_db)):
    # Un job vivo no se borra: el worker seguiría gastando OpenAI sobre
    # una fila inexistente y el commit final reventaría (StaleDataError).
    job = history_service.get_job(db, job_id)
    if job is not None and job.status in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail="El job está en cola o en curso: cancélalo antes de borrarlo.",
        )
    if not history_service.delete_job(db, job_id):
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    # Borrar también el SRT archivado (si existe)
    from app.core.config import DATA_DIR
    try:
        (DATA_DIR / "outputs" / f"job_{job_id}.srt").unlink(missing_ok=True)
    except OSError:
        pass
    return Response(status_code=204)


@router.get("/jobs/{job_id}/download")
def download_job_srt(job_id: int, db: Session = Depends(get_db)):
    """Devuelve el SRT final archivado de un job completado.

    Solo disponible para jobs posteriores a v3.5.1 (cuando se empezó a
    archivar el output); para jobs antiguos devuelve 404 con mensaje claro.
    """
    from app.core.config import DATA_DIR

    job = history_service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado.")

    srt_path = DATA_DIR / "outputs" / f"job_{job_id}.srt"
    if not srt_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Este proyecto es anterior al archivado de resultados "
                   "y su SRT ya no está disponible para descarga.",
        )
    return Response(
        content=srt_path.read_text(encoding="utf-8"),
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="traducido_job_{job_id}.srt"',
        },
    )


# ══════════════════════════════════════════════════════════════════════════
# JOB LOGS  (polling en vivo desde el frontend, por uuid del worker)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/jobs/by-uuid/{job_uuid}/logs")
def get_job_logs_endpoint(job_uuid: str, since: int = 0):
    """Devuelve los logs acumulados del job con seq >= since.

    El frontend hace polling con `since = último seq visto + 1` para
    paginar incrementalmente sin recibir las líneas ya pintadas.
    """
    return {"logs": job_logs.get(job_uuid, since=since)}


# ══════════════════════════════════════════════════════════════════════════
# SYSTEM  (operaciones de mantenimiento de la instalación)
# ══════════════════════════════════════════════════════════════════════════
@router.post("/system/request-update", status_code=202)
def request_update():
    """Solicita la actualización de la instalación.

    El contenedor no puede reconstruirse a sí mismo, así que esto solo
    deja un archivo-señal en el volumen de datos. Una tarea programada
    del HOST (scripts/atender_actualizacion.ps1, cada 5 min) detecta la
    señal y ejecuta scripts/actualizar.ps1. Idempotente: pulsar varias
    veces no encola varias actualizaciones.
    """
    import time as _time

    from app.core.config import DATA_DIR

    flag = DATA_DIR / "update-requested"
    flag.write_text(str(_time.time()), encoding="utf-8")
    return {
        "scheduled": True,
        "detail": "Actualización solicitada; se aplicará en los próximos minutos.",
    }


# ══════════════════════════════════════════════════════════════════════════
# OCR  (detección y lectura de texto incrustado en frames de vídeo)
# ══════════════════════════════════════════════════════════════════════════
_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".avi")
_MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Cancelación cooperativa del OCR: la UI marca el uuid y el callback de
# progreso aborta en el siguiente frame. Dict uuid→timestamp con purga:
# cancelar un uuid que no corre (o tarde) dejaba la entrada para siempre
# (fuga lenta + cancelación fantasma si el uuid se reutilizara).
_OCR_CANCELLED: dict[str, float] = {}
_OCR_CANCEL_TTL_S = 3600.0


def _purgar_cancelaciones_ocr() -> None:
    import time as _time
    ahora = _time.monotonic()
    caducados = [u for u, t in _OCR_CANCELLED.items() if ahora - t > _OCR_CANCEL_TTL_S]
    for uuid in caducados:
        _OCR_CANCELLED.pop(uuid, None)


class _OcrCancelled(Exception):
    """Señal interna: el usuario canceló la detección en curso."""


@router.post("/ocr/cancel/{job_uuid}", status_code=202)
def ocr_cancel(job_uuid: str):
    """Marca una detección OCR para cancelación.

    El corte ocurre al terminar el frame en curso (~1-10 s después),
    no instantáneamente: el OCR de un frame no es interrumpible.
    """
    import time as _time
    _purgar_cancelaciones_ocr()
    _OCR_CANCELLED[job_uuid] = _time.monotonic()
    job_logs.log(job_uuid, "✖ Cancelación solicitada…", level="warn")
    return {"cancelling": True}


@router.post("/ocr/detect")
def ocr_detect(
    file: UploadFile = File(...),
    interval_s: float = Form(3.0, ge=0.5, le=60.0),
    min_confidence: float = Form(0.4, ge=0.0, le=1.0),
    translate: bool = Form(False),
    job_uuid: str = Form(""),
):
    """Detecta y lee texto incrustado en los frames de un vídeo.

    Endpoint síncrono a propósito: FastAPI lo ejecuta en su threadpool,
    así el OCR (CPU/GPU-bound, minutos) no bloquea el event loop y el
    polling de logs sigue respondiendo. El progreso por frame se publica
    en job_logs bajo `job_uuid` (mismo mecanismo que la traducción).

    Devuelve detecciones con thumbnail JPEG como base64 (el shape es el
    de ocr_service.read_text_in_frames, serializado para HTTP).
    """
    import asyncio
    import base64
    import tempfile
    from pathlib import Path as _Path

    from app.services import ocr_service

    if not file.filename.lower().endswith(_VIDEO_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado. Acepta: {', '.join(_VIDEO_EXTENSIONS)}",
        )

    _purgar_cancelaciones_ocr()

    tmp_path = None
    try:
        # Copia por streaming con tope: file.file.read() completo traía
        # hasta 2 GB a RAM por request (y el archivo ENTERO antes del 413)
        # en el mismo contenedor que carga EasyOCR/torch — OOM esperando.
        bytes_total = 0
        with tempfile.NamedTemporaryFile(suffix=_Path(file.filename).suffix,
                                         delete=False) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_total += len(chunk)
                if bytes_total > _MAX_VIDEO_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Vídeo demasiado grande (máx 2 GB).",
                    )
                tmp.write(chunk)

        job_logs.log(job_uuid, f"📥 Vídeo recibido · {bytes_total / 1024 / 1024:.1f} MB")

        job_logs.log(job_uuid, f"🎞 Extrayendo frames (1 cada {interval_s:.1f}s)…")
        frames = ocr_service.extract_frames(tmp_path, interval_s)
        job_logs.log(job_uuid, f"🎞 {len(frames)} frames extraídos · cargando EasyOCR…")

        def _on_progress(done: int, total: int) -> None:
            if job_uuid and job_uuid in _OCR_CANCELLED:
                raise _OcrCancelled()
            # Cada frame: la UI usa estas líneas para pintar la barra real.
            job_logs.log(job_uuid, f"🔍 OCR frame {done}/{total}")

        try:
            detections = ocr_service.read_text_in_frames(
                frames,
                progress_callback=_on_progress,
                min_confidence=min_confidence,
                filter_subtitle_zone=True,
            )
        except _OcrCancelled:
            job_logs.log(job_uuid, "✖ OCR cancelado por el usuario", level="warn")
            return {"detections": [], "cancelled": True}
        job_logs.log(job_uuid, f"🔍 OCR completo · {len(detections)} frames con texto")

        if detections and translate:
            job_logs.log(job_uuid, f"🌐 Traduciendo {len(detections)} textos (gpt-4o-mini)…")
            try:
                # Estamos en un worker thread del threadpool (def síncrono):
                # no hay event loop aquí, asyncio.run es seguro.
                detections = asyncio.run(ocr_service.translate_detections(detections))
            except Exception as e:
                job_logs.log(job_uuid, f"⚠ Traducción OCR falló: {e}", level="warn")
                for d in detections:
                    d["text_translated"] = ""

        job_logs.log(job_uuid, "✅ OCR terminado")
        return {
            "detections": [
                {
                    **{k: v for k, v in d.items() if k != "thumbnail"},
                    "thumbnail_b64": base64.b64encode(d["thumbnail"]).decode("ascii"),
                }
                for d in detections
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        job_logs.log(job_uuid, f"✖ OCR falló: {e}", level="error")
        raise HTTPException(status_code=500, detail=f"Error en OCR: {str(e)[:200]}")
    finally:
        _OCR_CANCELLED.pop(job_uuid, None)
        if tmp_path:
            try:
                _Path(tmp_path).unlink()
            except OSError:
                pass
