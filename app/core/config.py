"""
Configuración centralizada del proyecto.
Todas las rutas y parámetros ajustables viven aquí.
Se pueden sobreescribir via variables de entorno (.env).
"""
import os
from pathlib import Path

# ── Rutas base ────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"

# Crear los directorios si no existen (idempotente)
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ── Base de datos ─────────────────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{DATA_DIR / 'subtitulam.db'}",
)

# ── OpenAI ────────────────────────────────────────────────────────────────
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
OPENAI_MAX_TOKENS  = int(os.getenv("OPENAI_MAX_TOKENS", "800"))

# ── Traducción ────────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE  = int(os.getenv("DEFAULT_CHUNK_SIZE", "5"))
DEFAULT_CPL_LIMIT   = int(os.getenv("DEFAULT_CPL_LIMIT", "38"))
DEFAULT_TARGET_LANG = os.getenv("DEFAULT_TARGET_LANG", "es")
