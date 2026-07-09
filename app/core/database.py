"""
Motor SQLAlchemy + factory de sesiones + Base declarativa.
Importa desde aquí para hablar con la base de datos.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import DATABASE_URL

# ── Engine ────────────────────────────────────────────────────────────────
# La "libreta" — conexión al archivo SQLite. Una sola para toda la app.
# check_same_thread=False: obligatorio para SQLite + FastAPI (multihilo).
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)

if DATABASE_URL.startswith("sqlite"):
    # WAL: lectores concurrentes con un escritor (el journal por defecto
    # da "database is locked" en cuanto un job inserta ~1.500 Translations
    # mientras otro request lista el historial). busy_timeout espera en
    # vez de fallar; synchronous=NORMAL es el punto seguro con WAL.
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

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
