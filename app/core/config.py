"""
Configuración centralizada del proyecto.
Todas las rutas y parámetros ajustables viven aquí.
Se pueden sobreescribir vía variables de entorno (.env).

Este módulo es la ÚNICA lectura del entorno: carga .env por sí mismo
para que el comportamiento sea idéntico en local (`uv run uvicorn`) y
en Docker (donde compose inyecta las vars y load_dotenv es no-op).
"""
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Versión (única fuente: pyproject.toml vía package metadata) ───────────
try:
    APP_VERSION = _pkg_version("ai-subtitle-translator")
except PackageNotFoundError:
    APP_VERSION = "dev"  # ejecutando sin instalar el paquete

# ── Rutas base ────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"

# Crear el directorio si no existe (idempotente)
DATA_DIR.mkdir(exist_ok=True)

# ── Base de datos ─────────────────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{DATA_DIR / 'subtitulam.db'}",
)

# ── Qdrant ────────────────────────────────────────────────────────────────
QDRANT_URL     = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None  # None si self-hosted sin auth

# ── OpenAI ────────────────────────────────────────────────────────────────
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
OPENAI_MAX_TOKENS  = int(os.getenv("OPENAI_MAX_TOKENS", "800"))

# ── Traducción ────────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE  = int(os.getenv("DEFAULT_CHUNK_SIZE", "5"))
DEFAULT_CPL_LIMIT   = int(os.getenv("DEFAULT_CPL_LIMIT", "38"))
DEFAULT_TARGET_LANG = os.getenv("DEFAULT_TARGET_LANG", "es")
