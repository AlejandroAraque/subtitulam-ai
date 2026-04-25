"""
Capa de servicio del histórico — persistencia atómica de Jobs y sus
Translations. Esta capa es el punto de extensión para v2 (RAG):
cuando indexemos en ChromaDB, lo haremos aquí, sin tocar los endpoints.
"""
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.schemas import Job, Translation


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

def save_completed_job(
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
    Crea un Job y todas sus Translations en una sola transacción.
    All-or-nothing: si algo falla, rollback completo.

    cues_source / cues_target: diccionarios {cue_idx: texto}.
    El servicio crea una Translation por cada índice presente en cues_source.
    """
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

        # PUNTO DE EXTENSIÓN PARA v2 (RAG):
        # aquí indexaremos cada (source_text, target_text) en ChromaDB
        # antes del commit. Si falla la indexación, rollback de todo.

        db.commit()
        db.refresh(job)
        return job

    except Exception:
        db.rollback()
        raise


def delete_job(db: Session, job_id: int) -> bool:
    """Borra un job y, en cascada, todas sus translations."""
    job = db.get(Job, job_id)
    if job is None:
        return False
    db.delete(job)
    db.commit()
    return True
