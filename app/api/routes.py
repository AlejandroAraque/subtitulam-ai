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

    try:
        text_content = content.decode("utf-8")
        original_subtitles = srt_service.parse_srt(text_content)
        cues_source = {s.index: s.content for s in original_subtitles}
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
        try:
            result = await translation_service.translate_texts(
                cues_source,
                target_lang=target_lang,
                context=context,
                cpl_limit=cpl,
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

        # ── 3. Reconstruir el SRT respetando timecodes originales ─────────
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
    return Response(status_code=204)


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
# OCR  (detección y lectura de texto incrustado en frames de vídeo)
# ══════════════════════════════════════════════════════════════════════════
_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv", ".avi")
_MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


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

    content = file.file.read()
    if len(content) > _MAX_VIDEO_BYTES:
        raise HTTPException(status_code=413, detail="Vídeo demasiado grande (máx 2 GB).")

    job_logs.log(job_uuid, f"📥 Vídeo recibido · {len(content) / 1024 / 1024:.1f} MB")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=_Path(file.filename).suffix,
                                         delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        del content  # liberar la copia en RAM cuanto antes

        job_logs.log(job_uuid, f"🎞 Extrayendo frames (1 cada {interval_s:.1f}s)…")
        frames = ocr_service.extract_frames(tmp_path, interval_s)
        job_logs.log(job_uuid, f"🎞 {len(frames)} frames extraídos · cargando EasyOCR…")

        def _on_progress(done: int, total: int) -> None:
            if done == 1 or done % 10 == 0 or done == total:
                job_logs.log(job_uuid, f"🔍 OCR frame {done}/{total}")

        detections = ocr_service.read_text_in_frames(
            frames,
            progress_callback=_on_progress,
            min_confidence=min_confidence,
            filter_subtitle_zone=True,
        )
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
        if tmp_path:
            try:
                _Path(tmp_path).unlink()
            except OSError:
                pass
