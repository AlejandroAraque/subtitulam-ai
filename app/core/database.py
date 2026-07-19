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


# ── Migración ligera ──────────────────────────────────────────────────────
# create_all NO altera tablas existentes: cada columna nueva sobre una BBDD
# de producción (la del piloto tiene datos irreemplazables) necesitaba un
# ALTER manual dentro del volumen Docker. Este mini-migrador cubre el caso
# aditivo (columnas nuevas) de forma idempotente. Si algún día hace falta
# renombrar/borrar columnas o migrar a Postgres, el salto es Alembic.
_JOB_COLUMNS_NUEVAS = {
    # nombre → definición SQL (SQLite exige default constante en ADD COLUMN)
    "job_uuid":       "VARCHAR(64) NOT NULL DEFAULT ''",
    "auto_context":   "BOOLEAN NOT NULL DEFAULT 0",
    "finished_at":    "DATETIME",
    "failed_cues":    "INTEGER NOT NULL DEFAULT 0",
    "cps_violations": "INTEGER NOT NULL DEFAULT 0",
    "n_cues":         "INTEGER NOT NULL DEFAULT 0",
}


def _ensure_schema(target_engine=None) -> None:
    """Añade a `jobs` las columnas que falten (ALTER TABLE aditivo).

    OJO: pysqlite autocommitea DDL, así que los ALTER NO son atómicos
    con el resto — si el proceso muere a mitad, las columnas quedan y el
    'if col in anadidas' del arranque siguiente se saltaría el resto.
    Por eso el backfill y el índice son INCONDICIONALES e idempotentes:
    cuestan ~0 cuando no hay nada que hacer y se auto-reparan solos.
    """
    from sqlalchemy import text

    with (target_engine or engine).begin() as conn:
        existentes = {
            row[1]  # (cid, name, type, notnull, default, pk)
            for row in conn.execute(text("PRAGMA table_info(jobs)"))
        }
        if not existentes:
            return  # tabla recién creada por create_all: ya está completa

        for col, ddl in _JOB_COLUMNS_NUEVAS.items():
            if col not in existentes:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}"))

        # Backfill idempotente: solo toca completados con n_cues sin
        # rellenar (jobs anteriores a la columna desnormalizada).
        conn.execute(text(
            "UPDATE jobs SET n_cues = ("
            "  SELECT COUNT(*) FROM translations"
            "  WHERE translations.job_id = jobs.id)"
            " WHERE n_cues = 0 AND status = 'completed'"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_jobs_job_uuid ON jobs (job_uuid)"
        ))


# ── Inicializador ─────────────────────────────────────────────────────────
def init_db() -> None:
    """
    Crea todas las tablas que no existan e incorpora las columnas nuevas
    a las existentes. Idempotente — no destruye datos.
    Se llamará desde el lifespan de FastAPI (main.py).
    """
    # Import local para evitar dependencia circular: schemas.py importa Base
    # desde aquí, y Base.metadata necesita conocer los modelos antes de crear.
    from app.models import schemas  # noqa: F401 — registra las tablas
    Base.metadata.create_all(bind=engine)
    _ensure_schema()
