"""
Capa de servicio del histórico — persistencia atómica de Jobs y sus
Translations + indexación en ChromaDB para retrieval futuro (v2.2+).
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.schemas import Job, Translation
from app.services import rag_service

logger = logging.getLogger("subtitulam.history")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ── Lectura ───────────────────────────────────────────────────────────────

def list_jobs(db: Session, limit: int = 50) -> List[Job]:
    """Devuelve los jobs más recientes primero, hasta `limit`."""
    stmt = select(Job).order_by(Job.started_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def get_job(db: Session, job_id: int) -> Optional[Job]:
    return db.get(Job, job_id)


def get_job_translations(db: Session, job_id: int) -> List[Translation]:
    """Devuelve las translations de un job, ordenadas por cue_idx."""
    stmt = (
        select(Translation)
        .where(Translation.job_id == job_id)
        .order_by(Translation.cue_idx.asc())
    )
    return list(db.scalars(stmt).all())


# ── Escritura ─────────────────────────────────────────────────────────────

async def save_completed_job(
    db: Session,
    *,
    filename: str,
    target_lang: str,
    cpl: int,
    context: str,
    elapsed_s: float,
    cpl_compliance: float,
    tokens_prompt: int,
    tokens_completion: int,
    cues_source: Dict[int, str],
    cues_target: Dict[int, str],
    status: str = "completed",
    error: str = "",
) -> Job:
    """
    Crea un Job + todas sus Translations en una sola transacción SQLite,
    y luego indexa los pares (source_text, target_text) en ChromaDB para
    retrieval futuro.

    Política de errores:
      - SQLite: all-or-nothing (rollback si falla cualquier paso del INSERT).
      - ChromaDB (hook): best-effort. Si la indexación falla, se loguea pero
        NO se hace rollback de SQLite — la traducción ya está completada y
        devuelta al usuario; perderla por un fallo del lado RAG sería peor
        que dejar el job sin indexar (recuperable con un backfill).
    """
    # ── 1. Persistencia SQLite (transaccional) ───────────────────────────
    try:
        job = Job(
            filename=filename,
            target_lang=target_lang,
            cpl=cpl,
            context=context,
            elapsed_s=elapsed_s,
            cpl_compliance=cpl_compliance,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            status=status,
            error=error,
        )
        db.add(job)
        db.flush()   # asigna job.id sin commit todavía

        for cue_idx, src_text in cues_source.items():
            tr = Translation(
                job_id=job.id,
                cue_idx=cue_idx,
                source_text=src_text,
                target_text=cues_target.get(cue_idx, ""),
            )
            db.add(tr)

        db.commit()
        db.refresh(job)

    except Exception:
        db.rollback()
        raise

    # ── 2. Hook RAG: indexar en ChromaDB (best-effort, no bloquea) ───────
    if status == "completed":
        try:
            translations_for_index = [
                {
                    "id":          t.id,
                    "cue_idx":     t.cue_idx,
                    "source_text": t.source_text,
                    "target_text": t.target_text,
                    "target_lang": job.target_lang,
                }
                for t in job.translations
            ]
            n = await rag_service.add_translations(
                job_id=job.id,
                translations=translations_for_index,
            )
            logger.info("Job %d · indexadas %d translations en ChromaDB", job.id, n)
        except Exception as e:
            logger.warning(
                "Job %d · fallo indexando en ChromaDB (no bloqueante): %s",
                job.id, e,
            )

    return job


def delete_job(db: Session, job_id: int) -> bool:
    """Borra un job y, en cascada, todas sus translations."""
    job = db.get(Job, job_id)
    if job is None:
        return False
    db.delete(job)
    db.commit()
    return True
