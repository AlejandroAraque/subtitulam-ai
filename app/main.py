import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import APP_VERSION
from app.core.database import SessionLocal, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Hooks de arranque y apagado del servidor.
    Al arrancar: crea/actualiza el esquema SQLite (idempotente), marca como
    failed los jobs 'running' que un reinicio dejó zombis (los 'queued'
    sobreviven: la cola es persistente) y arranca el worker de la cola.
    Al apagar: detiene el worker limpiamente.

    SUBTITULAM_DISABLE_WORKER=1 desactiva el worker (tests, herramientas).
    """
    init_db()

    from app.services import history_service
    db = SessionLocal()
    try:
        history_service.recover_interrupted_jobs(db)
    finally:
        db.close()

    worker_task = None
    if not os.getenv("SUBTITULAM_DISABLE_WORKER"):
        from app.services import job_runner
        # Supervisado: si el worker muriera por un BaseException raro
        # (MemoryError…), se relanza en vez de dejar la cola parada en
        # silencio mientras el backend sigue aceptando encolados.
        worker_task = asyncio.create_task(job_runner.supervised_worker_loop())

    yield

    if worker_task is not None:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task


app = FastAPI(
    title="Subtitulam API",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Sin middleware CORS a propósito: la API no se consume desde navegadores.
# Streamlit la llama server-side con requests; añadir CORS "*" solo ampliaba
# la superficie para que cualquier web visitada pudiera llamar a la API local.

app.include_router(router)
