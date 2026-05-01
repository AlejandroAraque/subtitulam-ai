from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import (
    srt_service,
    translation_service,
    glossary_service,
    history_service,
)

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════
# ROOT
# ══════════════════════════════════════════════════════════════════════════
@router.get("/")
def root():
    return {"message": "API lista para traducir con GPT-4o 🤖", "version": "1.5.0"}


# ══════════════════════════════════════════════════════════════════════════
# TRANSLATE — ahora persiste cada job + sus traducciones en SQLite
# ══════════════════════════════════════════════════════════════════════════
@router.post("/translate")
async def translate_subtitle(
    file: UploadFile = File(...),
    context: str = Form(""),
    target_lang: str = Form("es"),
    cpl: int = Form(38),
    db: Session = Depends(get_db),
):
    """Traduce un .srt y persiste el job y sus cues en SQLite."""
    if not file.filename.endswith(".srt"):
        raise HTTPException(status_code=400, detail="Sube un archivo .srt válido.")

    content = await file.read()

    try:
        text_content = content.decode("utf-8")
        original_subtitles = srt_service.parse_srt(text_content)
        cues_source = {s.index: s.content for s in original_subtitles}

        # ── Traducción ────────────────────────────────────────────────────
        result = await translation_service.translate_texts(
            cues_source,
            target_lang=target_lang,
            context=context,
            cpl_limit=cpl,
        )
        cues_target = result["translations"]

        # ── Reconstruir el SRT respetando timecodes originales ────────────
        final_texts = [cues_target.get(s.index, s.content) for s in original_subtitles]
        final_srt_content = srt_service.rebuild_srt(original_subtitles, final_texts)

        # ── Calcular CPL compliance real ──────────────────────────────────
        all_lines = []
        for txt in cues_target.values():
            all_lines.extend([ln for ln in txt.split("\n") if ln.strip()])
        n_total = max(1, len(all_lines))
        n_under = sum(1 for ln in all_lines if len(ln) <= cpl)
        cpl_compliance = round(n_under / n_total * 100, 1)

        # ── Persistir Job + Translations atómicamente ─────────────────────
        await history_service.save_completed_job(
            db,
            filename=file.filename,
            target_lang=target_lang,
            cpl=cpl,
            context=context,
            elapsed_s=result["elapsed_s"],
            cpl_compliance=cpl_compliance,
            tokens_prompt=result["tokens_prompt"],
            tokens_completion=result["tokens_completion"],
            cues_source=cues_source,
            cues_target=cues_target,
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
            },
        )

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
