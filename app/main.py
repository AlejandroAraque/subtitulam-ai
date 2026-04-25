from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
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
    version="1.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
