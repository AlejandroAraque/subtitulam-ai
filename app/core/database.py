"""
Motor SQLAlchemy + factory de sesiones + Base declarativa.
Importa desde aquí para hablar con la base de datos.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.core.config import DATABASE_URL

# ── Engine ────────────────────────────────────────────────────────────────
# La "libreta" — conexión al archivo SQLite. Una sola para toda la app.
# check_same_thread=False: obligatorio para SQLite + FastAPI (multihilo).
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)

# ── Session factory ───────────────────────────────────────────────────────
# Cada petición pide una sesión nueva con SessionLocal(),
# trabaja, y la cierra. Aislamiento entre peticiones.
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,    # no flush automático tras cada query
    autocommit=False,   # commit explícito → transacciones manuales
    future=True,
)


# ── Base declarativa ──────────────────────────────────────────────────────
# Plantilla común de la que heredan todas las tablas.
# SQLAlchemy 2.x usa DeclarativeBase (sustituye al antiguo declarative_base()).
class Base(DeclarativeBase):
    pass


# ── Dependencia para FastAPI ──────────────────────────────────────────────
def get_db():
    """
    Yielded session por petición. FastAPI la cierra al terminar.
    Uso: `def endpoint(db: Session = Depends(get_db)):`
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Inicializador ─────────────────────────────────────────────────────────
def init_db() -> None:
    """
    Crea todas las tablas que no existan. Idempotente — no destruye datos.
    Se llamará desde el lifespan de FastAPI (main.py).
    """
    # Import local para evitar dependencia circular: schemas.py importa Base
    # desde aquí, y Base.metadata necesita conocer los modelos antes de crear.
    from app.models import schemas  # noqa: F401 — registra las tablas
    Base.metadata.create_all(bind=engine)
