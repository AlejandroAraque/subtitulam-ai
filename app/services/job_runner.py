"""Worker de la cola de traducción (patrón 202+polling, v3.8).

POST /translate ya NO traduce: valida, guarda el SRT en data/uploads y
crea un Job 'queued' en SQLite. Este módulo es el consumidor: un task
asyncio único (arrancado en el lifespan) drena la cola en orden de
llegada, ejecuta el pipeline completo y persiste el resultado.

Qué elimina respecto al flujo anterior (request HTTP síncrono de hasta
3 h + worker thread en Streamlit):
  - jobs zombis si el frontend se reinicia o la red parpadea a mitad de
    película (el backend seguía gastando ~1 $/película sin dueño);
  - la cola se pierde con el proceso Streamlit — ahora sobrevive a
    reinicios (los 'queued' se retoman; los 'running' interrumpidos se
    marcan failed en el recovery del arranque);
  - el resultado solo existía en la respuesta HTTP — ahora se archiva
    ANTES de marcar completed y se descarga de /jobs/{id}/download.

Restricción heredada y documentada: la cancelación usa un dict en
memoria de proceso (como job_logs). NO añadir --workers al uvicorn.
"""
import asyncio
import logging
from pathlib import Path

from app.core import job_logs
from app.core.database import SessionLocal
from app.services import (
    context_service,
    glossary_service,
    history_service,
    srt_service,
    translation_service,
)

logger = logging.getLogger("subtitulam.job_runner")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# uuids marcados para cancelar, con timestamp para purga (mismo patrón
# que _OCR_CANCELLED): sin TTL, cancelar un job 'queued' dejaba la
# entrada para siempre y un uuid reutilizado se auto-cancelaba fantasma.
CANCELLED_UUIDS: dict[str, float] = {}
_CANCEL_TTL_S = 6 * 3600.0

_POLL_INTERVAL_S = 1.5


def mark_cancel(job_uuid: str) -> None:
    """Marca un uuid para cancelación cooperativa (purga entradas viejas)."""
    import time as _time
    ahora = _time.monotonic()
    caducados = [u for u, t in CANCELLED_UUIDS.items() if ahora - t > _CANCEL_TTL_S]
    for u in caducados:
        CANCELLED_UUIDS.pop(u, None)
    CANCELLED_UUIDS[job_uuid] = ahora


def upload_path(job_uuid: str) -> Path:
    """Ruta del SRT encolado. Se conserva tras el job (habilita futuros
    're-traducir con el motor actual' y diagnóstico de fallos)."""
    from app.core.config import DATA_DIR
    uploads = DATA_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    return uploads / f"{job_uuid}.srt"


def output_path(job_id: int) -> Path:
    """Ruta del SRT final archivado (la que sirve /jobs/{id}/download)."""
    from app.core.config import DATA_DIR
    outputs = DATA_DIR / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    return outputs / f"job_{job_id}.srt"


def _force_fail(job_id: int, error: str, session_factory) -> None:
    """Último recurso: marcar failed con una sesión FRESCA cuando la
    sesión del job no pudo (p. ej. 'database is locked' sostenido)."""
    try:
        db = session_factory()
        try:
            job = history_service.get_job(db, job_id)
            if job is not None and job.status == "running":
                history_service.fail_job(db, job, error[:2000])
        finally:
            db.close()
    except Exception:
        logger.exception("No se pudo marcar failed el job %d ni con sesión nueva", job_id)


async def execute_job(job_id: int, *, session_factory=SessionLocal) -> str:
    """Ejecuta un job encolado de principio a fin. Devuelve el estado
    final: completed | failed | cancelled | skipped.

    Invariante (hallazgo x3 de la revisión adversarial): NINGUNA
    excepción puede salir de aquí dejando el job en 'running' — todo lo
    posterior a mark_running está dentro de la misma red try/except.
    """
    db = session_factory()
    job_uuid = ""
    try:
        job = history_service.get_job(db, job_id)
        if job is None or job.status != "queued":
            if job is not None:
                CANCELLED_UUIDS.pop(job.job_uuid, None)
            return "skipped"
        job_uuid = job.job_uuid

        # Cancelado mientras esperaba en cola (carrera con el endpoint)
        if job_uuid in CANCELLED_UUIDS:
            history_service.mark_cancelled(db, job)
            return "cancelled"

        history_service.mark_running(db, job)

        try:
            text_content = upload_path(job_uuid).read_text(encoding="utf-8")
            # Normalizado también aquí (el enqueue ya normaliza): el worker
            # no puede depender de lo que haya en uploads/.
            original_subtitles = srt_service.parse_srt_normalizado(text_content)
            cues_source = {s.index: s.content for s in original_subtitles}
            # Clamp a ≥0: timecodes corruptos (end<start) darían
            # presupuestos CPS absurdos.
            cues_duration = {
                s.index: max(0.0, (s.end - s.start).total_seconds())
                for s in original_subtitles
            }

            # ── 0. Auto-context (opt-in, solo si no hay contexto manual) ──
            context = job.context
            if job.auto_context and not context.strip():
                job_logs.log(job_uuid, "🧠 Auto-context: generando contexto a partir del título…")
                context = await context_service.generate_context_from_title(job.filename)
                job.context = context
                db.commit()
                job_logs.log(job_uuid, f"🧠 Auto-context listo ({len(context)} caracteres)")

            # ── 1. Glosario actual (reglas obligatorias en el prompt) ─────
            glossary_terms = [t.to_dict() for t in glossary_service.list_terms(db)]
            job_logs.log(
                job_uuid,
                f"📚 Glosario cargado · {len(glossary_terms)} términos inyectados en el prompt",
            )

            # ── 2. Traducir con RAG + sliding window + glosario activos ───
            from app.core.config import DEFAULT_CHUNK_SIZE
            result = await translation_service.translate_texts(
                cues_source,
                chunk_size=DEFAULT_CHUNK_SIZE,
                target_lang=job.target_lang,
                context=context,
                cpl_limit=job.cpl,
                durations=cues_duration,
                job_id=job.id,
                filename=job.filename,
                use_rag=True,
                sliding_window_size=20,
                glossary=glossary_terms,
                job_uuid=job_uuid,
                cancel_check=lambda: job_uuid in CANCELLED_UUIDS,
            )

            cues_target = result["translations"]

            # ── 3. Política ante cues fallidas: >20% → no utilizable ──────
            n_failed = result.get("n_failed", 0)
            n_cues = max(1, len(cues_source))
            if n_failed / n_cues > 0.20:
                msg = (f"Traducción fallida: {n_failed}/{n_cues} cues con error "
                       f"(¿API key inválida o rate limit sostenido?)")
                history_service.fail_job(db, job, msg)
                job_logs.log(job_uuid, f"✖ {msg}", level="error")
                return "failed"

            # ── 4. Reconstruir el SRT respetando timecodes originales ─────
            final_texts = [cues_target.get(s.index, s.content) for s in original_subtitles]
            final_srt_content = srt_service.rebuild_srt(original_subtitles, final_texts)

            # ── 5. CPL compliance real ────────────────────────────────────
            all_lines = []
            for txt in cues_target.values():
                all_lines.extend([ln for ln in txt.split("\n") if ln.strip()])
            n_total = max(1, len(all_lines))
            n_under = sum(1 for ln in all_lines if len(ln) <= job.cpl)
            cpl_compliance = round(n_under / n_total * 100, 1)

            # ── 6. Archivar ANTES de completar: la UI descarga en cuanto
            # ve status=completed — el archivo tiene que existir ya. ──────
            output_path(job.id).write_text(final_srt_content, encoding="utf-8")

            # ── 7. Completar (bulk insert de ~1.500 filas + métricas).
            # En to_thread: bajo contención de SQLite el commit puede
            # esperar hasta busy_timeout (5 s) y eso NO puede congelar el
            # event loop que sirve el polling de la UI. ───────────────────
            await asyncio.to_thread(
                history_service.complete_job,
                db,
                job=job,
                cues_source=cues_source,
                cues_target=cues_target,
                elapsed_s=result["elapsed_s"],
                cpl_compliance=cpl_compliance,
                tokens_prompt=result["tokens_prompt"],
                tokens_completion=result["tokens_completion"],
                failed_cues=n_failed,
                cps_violations=result.get("n_cps_violations", 0),
            )
            job_logs.log(
                job_uuid,
                f"✅ Job completado · {result['elapsed_s']:.1f}s · "
                f"CPL compliance {cpl_compliance}% · "
                f"tokens prompt={result['tokens_prompt']} completion={result['tokens_completion']}",
            )
            return "completed"

        except translation_service.TranslationCancelled:
            history_service.mark_cancelled(db, job)
            return "cancelled"
        except Exception as e:
            # El SRT puede haber quedado archivado aunque no se persistiera
            # el job: decirlo evita rehacer un gasto ya hecho.
            sufijo = ""
            if output_path(job.id).is_file():
                sufijo = f" (el SRT sí quedó archivado en outputs/job_{job.id}.srt)"
            job_logs.log(job_uuid, f"✖ Error: {str(e)[:200]}{sufijo}", level="error")
            try:
                history_service.fail_job(db, job, f"{str(e)[:1900]}{sufijo}")
            except Exception:
                logger.exception("fail_job falló para el job %d; reintento con sesión nueva", job_id)
                _force_fail(job_id, f"{str(e)[:1900]}{sufijo}", session_factory)
            return "failed"
    finally:
        if job_uuid:
            CANCELLED_UUIDS.pop(job_uuid, None)
        db.close()


async def worker_loop(*, session_factory=SessionLocal) -> None:
    """Consumidor único de la cola: procesa jobs 'queued' en orden.

    Un solo job a la vez a propósito (Tier 1 de OpenAI: el paralelismo
    entre películas llega con el salto de tier — G8 del backlog)."""
    logger.info("Worker de cola de traducción arrancado")
    while True:
        try:
            db = session_factory()
            try:
                job_id = history_service.next_queued_job_id(db)
            finally:
                db.close()

            if job_id is None:
                await asyncio.sleep(_POLL_INTERVAL_S)
                continue

            estado = await execute_job(job_id, session_factory=session_factory)
            logger.info("Job %d → %s", job_id, estado)
        except asyncio.CancelledError:
            logger.info("Worker de cola detenido (apagado del servidor)")
            raise
        except Exception:
            # Un job con un error inesperado no puede tumbar el worker:
            # se registra y se sigue con la cola.
            logger.exception("Error inesperado en el worker de cola")
            await asyncio.sleep(5.0)


async def supervised_worker_loop(*, session_factory=SessionLocal) -> None:
    """Supervisor del worker: si worker_loop muere por un BaseException
    que no sea Exception (MemoryError, SystemExit de una librería…), lo
    relanza en vez de dejar la cola parada en silencio con el backend
    aceptando encolados que nadie procesaría."""
    while True:
        try:
            await worker_loop(session_factory=session_factory)
        except asyncio.CancelledError:
            raise
        except BaseException:
            logger.critical(
                "El worker de cola murió con un error no recuperable; "
                "se relanza en 10 s", exc_info=True,
            )
            await asyncio.sleep(10.0)
