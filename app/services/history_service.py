"""
Capa de servicio del histórico — persistencia atómica de Jobs y sus
Translations + indexación en Qdrant para retrieval futuro (v2.2+).

Desde v3.8 también es la cola de trabajos: un Job nace 'queued', el
worker del backend (app/services/job_runner.py) lo pasa a 'running' y
lo termina en 'completed' / 'failed' / 'cancelled'.
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import insert, select
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


def get_job_by_uuid(db: Session, job_uuid: str) -> Optional[Job]:
    """Job por el uuid del frontend (clave del polling y la cancelación)."""
    if not job_uuid:
        return None
    stmt = select(Job).where(Job.job_uuid == job_uuid).order_by(Job.id.desc())
    return db.scalars(stmt).first()


def get_jobs_by_uuids(db: Session, uuids: List[str]) -> List[Job]:
    """Jobs de una lista de uuids, en una sola query (snapshot de la UI)."""
    uuids = [u for u in uuids if u]
    if not uuids:
        return []
    stmt = select(Job).where(Job.job_uuid.in_(uuids))
    return list(db.scalars(stmt).all())


def list_active_jobs(db: Session) -> List[Job]:
    """Jobs vivos (queued o running), los queued en orden de llegada.

    La UI los muestra aunque la sesión del navegador se haya refrescado:
    la cola vive en el backend, un F5 ya no 'pierde' los trabajos.
    """
    stmt = (
        select(Job)
        .where(Job.status.in_(("queued", "running")))
        .order_by(Job.id.asc())
    )
    return list(db.scalars(stmt).all())


def next_queued_job_id(db: Session) -> Optional[int]:
    """id del job encolado más antiguo, o None si la cola está vacía."""
    stmt = (
        select(Job.id)
        .where(Job.status == "queued")
        .order_by(Job.id.asc())
        .limit(1)
    )
    return db.scalars(stmt).first()


# ── Escritura ─────────────────────────────────────────────────────────────

def create_queued_job(
    db: Session,
    *,
    filename: str,
    target_lang: str,
    cpl: int,
    context: str,
    job_uuid: str,
    auto_context: bool = False,
    n_cues: int = 0,
    status: str = "queued",
) -> Job:
    """Crea un Job en estado 'queued'. El worker del backend lo recogerá.

    n_cues se persiste ya al encolar: sin él, los jobs queued/failed
    mostraban '0 líneas' en el Historial aunque el dato se conociera.
    status='running' lo usan las herramientas que traducen fuera de la
    cola (eval/showcase) para que el worker jamás tome su job.

    Retorna el job ya con id asignado, para que translate_texts pueda usarlo
    como clave estable en Qdrant durante la indexación per-batch.
    """
    job = Job(
        filename=filename,
        target_lang=target_lang,
        cpl=cpl,
        context=context,
        job_uuid=job_uuid,
        auto_context=auto_context,
        n_cues=n_cues,
        status=status,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("Job %d encolado (%s)", job.id, filename)
    return job


def mark_running(db: Session, job: Job) -> Job:
    """El worker toma el job: queued → running. started_at pasa a ser el
    inicio REAL del procesado (no el momento de encolar)."""
    job.status = "running"
    job.started_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def mark_cancelled(db: Session, job: Job) -> Job:
    """Job cancelado por el usuario (en cola o en curso)."""
    job.status = "cancelled"
    job.finished_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    logger.info("Job %d cancelado por el usuario", job.id)
    return job


def recover_interrupted_jobs(db: Session) -> int:
    """Al arrancar el backend: los jobs que quedaron 'running' en un
    reinicio son zombis (el pipeline murió con el proceso) → 'failed'.
    Los 'queued' se conservan: el worker los procesará — la cola
    sobrevive al reinicio.
    """
    stmt = select(Job).where(Job.status == "running")
    zombis = list(db.scalars(stmt).all())
    for job in zombis:
        job.status = "failed"
        job.error = "Interrumpido por un reinicio del servidor"
        job.finished_at = datetime.utcnow()
    if zombis:
        db.commit()
        logger.warning(
            "Recovery al arrancar: %d job(s) 'running' marcados como failed",
            len(zombis),
        )
    return len(zombis)


def complete_job(
    db: Session,
    *,
    job: Job,
    cues_source: Dict[int, str],
    cues_target: Dict[int, str],
    elapsed_s: float,
    cpl_compliance: float,
    tokens_prompt: int,
    tokens_completion: int,
    failed_cues: int = 0,
    cps_violations: int = 0,
) -> Job:
    """Completa un Job pendiente: inserta sus Translations, actualiza
    métricas y cambia status a 'completed'.

    Síncrona a propósito (era `async def` sin ningún await): el caller
    async la ejecuta con asyncio.to_thread para que el bulk insert de
    ~1.500 filas nunca congele el event loop bajo contención de SQLite.

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
        job.failed_cues       = failed_cues
        job.cps_violations    = cps_violations
        job.n_cues            = len(cues_source)
        job.finished_at       = datetime.utcnow()
        job.status            = "completed"

        # Insertar Translations en bloque: db.add una a una eran ~1.500
        # INSERT individuales por película.
        filas = [
            {
                "job_id":      job.id,
                "cue_idx":     cue_idx,
                "source_text": src_text,
                "target_text": cues_target.get(cue_idx, ""),
                "created_at":  datetime.utcnow(),
            }
            for cue_idx, src_text in cues_source.items()
        ]
        if filas:
            db.execute(insert(Translation), filas)

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
    job.finished_at = datetime.utcnow()
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
