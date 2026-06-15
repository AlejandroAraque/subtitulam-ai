from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import APP_VERSION
from app.core.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Hooks de arranque y apagado del servidor.
    Al arrancar: crea tablas SQLite si no existen (idempotente, no borra datos).
    Al apagar: nada por ahora — aquí cerraríamos pools, websockets, etc.
    """
    init_db()
    yield


app = FastAPI(
    title="Subtitulam API",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Sin middleware CORS a propósito: la API no se consume desde navegadores.
# Streamlit la llama server-side con requests; añadir CORS "*" solo ampliaba
# la superficie para que cualquier web visitada pudiera llamar a la API local.

app.include_router(router)
