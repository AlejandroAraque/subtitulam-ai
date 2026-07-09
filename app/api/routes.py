import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import job_logs
from app.core.database import get_db
from app.services import (
    context_service,
    glossary_service,
    history_service,
    srt_service,
    translation_service,
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
# TRANSLATE — ahora persiste cada job + sus traducciones en SQLite
# ══════════════════════════════════════════════════════════════════════════
@router.post("/translate")
async def translate_subtitle(
    file: UploadFile = File(...),
    context: str = Form(""),
    target_lang: str = Form("es"),
    cpl: int = Form(38),
    auto_context: bool = Form(False),
    job_uuid: str = Form(""),  # uuid del frontend para logs en vivo (opcional)
    db: Session = Depends(get_db),
):
    """Traduce un .srt con RAG + sliding window, persiste el job y sus cues."""
    if not file.filename.endswith(".srt"):
        raise HTTPException(status_code=400, detail="Sube un archivo .srt válido.")

    content = await file.read()
    if len(content) > _MAX_SRT_BYTES:
        raise HTTPException(
            status_code=413,
            detail="SRT demasiado grande (máx 5 MB). Un largometraje ronda los 100 KB.",
        )

    try:
        text_content = content.decode("utf-8")
        original_subtitles = srt_service.parse_srt(text_content)
        cues_source = {s.index: s.content for s in original_subtitles}
        # Duración de cada cue en segundos: la usa el pipeline para la
        # pasada de velocidad de lectura (CPS). Clamp a ≥0: un SRT con
        # timecodes corruptos (end<start) daría presupuestos absurdos.
        cues_duration = {
            s.index: max(0.0, (s.end - s.start).total_seconds())
            for s in original_subtitles
        }
        job_logs.log(
            job_uuid,
            f"📥 SRT recibido · {len(content) / 1024:.1f} KB · "
            f"{len(cues_source)} cues · cpl={cpl} · {target_lang}",
        )

        # ── 0. Auto-context (opt-in, solo si el usuario no escribió contexto)
        if auto_context and not context.strip():
            job_logs.log(job_uuid, "🧠 Auto-context: generando contexto a partir del título…")
            context = await context_service.generate_context_from_title(file.filename)
            job_logs.log(job_uuid, f"🧠 Auto-context listo ({len(context)} caracteres)")

        # ── 1. Cargar glosario actual (reglas obligatorias en el prompt) ──
        glossary_terms = [t.to_dict() for t in glossary_service.list_terms(db)]
        job_logs.log(
            job_uuid,
            f"📚 Glosario cargado · {len(glossary_terms)} términos inyectados en el prompt",
        )

        # ── 2. Crear Job pendiente — necesitamos job.id para indexar batch ─
        job = history_service.create_pending_job(
            db,
            filename=file.filename,
            target_lang=target_lang,
            cpl=cpl,
            context=context,
        )
        job_logs.log(job_uuid, f"💾 Job persistido en BBDD · id={job.id}")

        # ── 3. Traducir con RAG + sliding window + glosario activos ───────
        from app.core.config import DEFAULT_CHUNK_SIZE
        try:
            result = await translation_service.translate_texts(
                cues_source,
                chunk_size=DEFAULT_CHUNK_SIZE,
                target_lang=target_lang,
                context=context,
                cpl_limit=cpl,
                durations=cues_duration,
                job_id=job.id,
                filename=file.filename,
                use_rag=True,
                sliding_window_size=20,
                glossary=glossary_terms,
                job_uuid=job_uuid,
            )
        except Exception as e:
            job_logs.log(job_uuid, f"✖ Error en traducción: {str(e)[:200]}", level="error")
            history_service.fail_job(db, job, str(e))
            raise

        cues_target = result["translations"]

        # ── 3a. Política ante cues fallidas (antes: fallo 100% silencioso) ─
        # Si falló más del 20% del archivo (típico: API key inválida, 429
        # en cascada), el job NO es un resultado utilizable: failed + 502.
        # Con fallos parciales seguimos, pero avisando vía X-Failed-Cues.
        n_failed = result.get("n_failed", 0)
        n_cues = max(1, len(cues_source))
        if n_failed / n_cues > 0.20:
            msg = (f"Traducción fallida: {n_failed}/{n_cues} cues con error "
                   f"(¿API key inválida o rate limit sostenido?)")
            history_service.fail_job(db, job, msg)
            job_logs.log(job_uuid, f"✖ {msg}", level="error")
            raise HTTPException(status_code=502, detail=msg)

        # ── 3b. Reconstruir el SRT respetando timecodes originales ────────
        final_texts = [cues_target.get(s.index, s.content) for s in original_subtitles]
        final_srt_content = srt_service.rebuild_srt(original_subtitles, final_texts)

        # ── 4. Calcular CPL compliance real ───────────────────────────────
        all_lines = []
        for txt in cues_target.values():
            all_lines.extend([ln for ln in txt.split("\n") if ln.strip()])
        n_total = max(1, len(all_lines))
        n_under = sum(1 for ln in all_lines if len(ln) <= cpl)
        cpl_compliance = round(n_under / n_total * 100, 1)

        # ── 5. Completar el Job (insertar Translations, status='completed') ─
        await history_service.complete_job(
            db,
            job=job,
            cues_source=cues_source,
            cues_target=cues_target,
            elapsed_s=result["elapsed_s"],
            cpl_compliance=cpl_compliance,
            tokens_prompt=result["tokens_prompt"],
            tokens_completion=result["tokens_completion"],
        )
        job_logs.log(
            job_uuid,
            f"✅ Job completado · {result['elapsed_s']:.1f}s · "
            f"CPL compliance {cpl_compliance}% · "
            f"tokens prompt={result['tokens_prompt']} completion={result['tokens_completion']}",
        )

        # ── 6. Persistir el SRT final a disco para re-descargas ───────────
        # Sin esto, el resultado solo existía en la respuesta HTTP: si el
        # usuario cerraba el navegador sin descargar, el trabajo (pagado)
        # quedaba inaccesible. La tabla Translation no guarda timecodes,
        # así que el archivo es la única forma barata de reconstruirlo.
        try:
            from app.core.config import DATA_DIR
            outputs_dir = DATA_DIR / "outputs"
            outputs_dir.mkdir(exist_ok=True)
            (outputs_dir / f"job_{job.id}.srt").write_text(
                final_srt_content, encoding="utf-8",
            )
        except OSError as e:
            # No bloqueante: la respuesta HTTP sigue llevando el SRT.
            job_logs.log(job_uuid, f"⚠ No se pudo archivar el SRT: {e}", level="warn")

        return Response(
            content=final_srt_content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f"attachment; filename=traducido_{file.filename}",
                "X-Cpl-Compliance":    str(cpl_compliance),
                "X-Tokens-Prompt":     str(result["tokens_prompt"]),
                "X-Tokens-Completion": str(result["tokens_completion"]),
                "X-Elapsed-Seconds":   f"{result['elapsed_s']:.2f}",
                "X-Job-Id":            str(job.id),
                "X-Failed-Cues":       str(n_failed),
                "X-Cps-Violations":    str(result.get("n_cps_violations", 0)),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el servidor: {str(e)}")


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
