"""
Capa de servicio del histórico — persistencia atómica de Jobs y sus
Translations + indexación en Qdrant para retrieval futuro (v2.2+).
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.schemas import Job, Translation

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

def create_pending_job(
    db: Session,
    *,
    filename: str,
    target_lang: str,
    cpl: int,
    context: str,
) -> Job:
    """Crea un Job en estado 'running' antes de empezar a traducir.

    Retorna el job ya con id asignado, para que translate_texts pueda usarlo
    como clave estable en Qdrant durante la indexación per-batch.
    """
    job = Job(
        filename=filename,
        target_lang=target_lang,
        cpl=cpl,
        context=context,
        status="running",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("Job %d creado en estado 'running' (%s)", job.id, filename)
    return job


async def complete_job(
    db: Session,
    *,
    job: Job,
    cues_source: Dict[int, str],
    cues_target: Dict[int, str],
    elapsed_s: float,
    cpl_compliance: float,
    tokens_prompt: int,
    tokens_completion: int,
) -> Job:
    """Completa un Job pendiente: inserta sus Translations, actualiza
    métricas y cambia status a 'completed'.

    La indexación en Qdrant ocurre batch a batch desde translate_texts
    (que además filtra las cues '[ERROR]'). Aquí NO se re-indexa: el
    antiguo "safety net" re-embebía el archivo entero al cerrar cada
    job (~1.200 requests duplicados por película) y, al no filtrar
    '[ERROR]', contaminaba el corpus RAG con cues fallidas. Para
    recuperar una indexación rota existe scripts/backfill_qdrant.py.
    """
    try:
        # Actualizar métricas en el Job
        job.elapsed_s         = elapsed_s
        job.cpl_compliance    = cpl_compliance
        job.tokens_prompt     = tokens_prompt
        job.tokens_completion = tokens_completion
        job.status            = "completed"

        # Insertar Translations
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

    return job


def fail_job(db: Session, job: Job, error: str) -> Job:
    """Marca un job pendiente como 'failed'. Sin Translations ni RAG."""
    job.status = "failed"
    job.error  = error
    db.commit()
    db.refresh(job)
    logger.warning("Job %d marcado como 'failed': %s", job.id, error[:80])
    return job


def delete_job(db: Session, job_id: int) -> bool:
    """Borra un job y, en cascada, todas sus translations."""
    job = db.get(Job, job_id)
    if job is None:
        return False
    db.delete(job)
    db.commit()
    return True
