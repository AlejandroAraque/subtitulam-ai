"""Tests de la cola de trabajos del backend (patrón 202+polling, v3.8).

Cubren el ciclo de vida completo: encolar (202 en vez de traducir en el
request), ejecutar (worker), cancelar (cooperativo), recovery tras
reinicio y las rutas nuevas de polling.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.services import history_service, job_runner, translation_service

SRT_MINI = """1
00:00:01,000 --> 00:00:03,000
Hello there.

2
00:00:04,000 --> 00:00:06,000
General Kenobi.
"""


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """BBDD SQLite temporal + DATA_DIR temporal, aislados del entorno real.

    TestClient se usa SIN context manager a propósito: así no corre el
    lifespan (ni worker ni init_db sobre la BBDD de desarrollo).
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    from app.models import schemas  # noqa: F401 — registra las tablas
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True,
    )
    monkeypatch.setattr("app.core.config.DATA_DIR", tmp_path)

    def _get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    yield factory, tmp_path
    app.dependency_overrides.clear()
    job_runner.CANCELLED_UUIDS.clear()


def _fake_translate_ok(texts_dict, **kwargs):
    async def _inner():
        return {
            "translations":      {i: f"ES {t}" for i, t in texts_dict.items()},
            "tokens_prompt":     100,
            "tokens_completion": 50,
            "elapsed_s":         0.5,
            "n_failed":          0,
            "n_cps_violations":  0,
        }
    return _inner()


# ── POST /translate: encola, no traduce ──────────────────────────────────

def test_translate_encola_y_responde_202(entorno):
    factory, tmp_path = entorno
    client = TestClient(app)
    r = client.post(
        "/translate",
        files={"file": ("mini.srt", SRT_MINI.encode("utf-8"), "text/plain")},
        data={"target_lang": "es", "cpl": "38", "job_uuid": "abc123def456"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_uuid"] == "abc123def456"

    db = factory()
    job = history_service.get_job(db, body["job_id"])
    assert job is not None and job.status == "queued"
    db.close()
    # El SRT queda guardado para el worker
    assert (tmp_path / "uploads" / "abc123def456.srt").is_file()


def test_translate_rechaza_archivo_invalido(entorno):
    client = TestClient(app)
    r = client.post(
        "/translate",
        files={"file": ("malo.srt", b"esto no es un srt", "text/plain")},
        data={"target_lang": "es", "cpl": "38"},
    )
    assert r.status_code == 400


def test_translate_rechaza_cpl_fuera_de_rango(entorno):
    client = TestClient(app)
    r = client.post(
        "/translate",
        files={"file": ("mini.srt", SRT_MINI.encode("utf-8"), "text/plain")},
        data={"target_lang": "es", "cpl": "99"},
    )
    assert r.status_code == 400


# ── Rutas de polling (orden de matching con /jobs/{job_id}) ──────────────

def test_jobs_active_no_choca_con_job_id(entorno):
    client = TestClient(app)
    r = client.get("/jobs/active")
    assert r.status_code == 200  # un 422 delataría el orden mal declarado
    assert r.json() == {"jobs": []}


def test_jobs_by_uuids_devuelve_solo_los_pedidos(entorno):
    factory, _ = entorno
    db = factory()
    for uuid in ("uno111", "dos222", "tres333"):
        history_service.create_queued_job(
            db, filename=f"{uuid}.srt", target_lang="es", cpl=38,
            context="", job_uuid=uuid,
        )
    db.close()
    client = TestClient(app)
    r = client.get("/jobs/by-uuids", params={"uuids": "uno111, tres333"})
    assert r.status_code == 200
    devueltos = {j["job_uuid"] for j in r.json()["jobs"]}
    assert devueltos == {"uno111", "tres333"}


# ── Worker: execute_job ──────────────────────────────────────────────────

def test_execute_job_completa(entorno, monkeypatch):
    factory, _ = entorno
    db = factory()
    job = history_service.create_queued_job(
        db, filename="mini.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidtest1",
    )
    job_id = job.id
    db.close()
    job_runner.upload_path("uuidtest1").write_text(SRT_MINI, encoding="utf-8")
    monkeypatch.setattr(translation_service, "translate_texts", _fake_translate_ok)

    estado = asyncio.run(job_runner.execute_job(job_id, session_factory=factory))
    assert estado == "completed"

    db = factory()
    job = history_service.get_job(db, job_id)
    assert job.status == "completed"
    assert job.n_cues == 2
    assert job.finished_at is not None
    assert len(history_service.get_job_translations(db, job_id)) == 2
    db.close()
    # El SRT final está archivado ANTES de marcar completed
    assert job_runner.output_path(job_id).is_file()


def test_execute_job_cancelado_cooperativamente(entorno, monkeypatch):
    factory, _ = entorno
    db = factory()
    job = history_service.create_queued_job(
        db, filename="mini.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidcancel",
    )
    job_id = job.id
    db.close()
    job_runner.upload_path("uuidcancel").write_text(SRT_MINI, encoding="utf-8")

    def _fake_cancelado(texts_dict, **kwargs):
        async def _inner():
            raise translation_service.TranslationCancelled()
        return _inner()

    monkeypatch.setattr(translation_service, "translate_texts", _fake_cancelado)
    estado = asyncio.run(job_runner.execute_job(job_id, session_factory=factory))
    assert estado == "cancelled"

    db = factory()
    assert history_service.get_job(db, job_id).status == "cancelled"
    db.close()


def test_execute_job_falla_con_mas_del_20pct_de_errores(entorno, monkeypatch):
    factory, _ = entorno
    db = factory()
    job = history_service.create_queued_job(
        db, filename="mini.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidfail",
    )
    job_id = job.id
    db.close()
    job_runner.upload_path("uuidfail").write_text(SRT_MINI, encoding="utf-8")

    def _fake_errores(texts_dict, **kwargs):
        async def _inner():
            return {
                "translations":      {i: f"[ERROR] {t}" for i, t in texts_dict.items()},
                "tokens_prompt":     0,
                "tokens_completion": 0,
                "elapsed_s":         0.1,
                "n_failed":          len(texts_dict),
                "n_cps_violations":  0,
            }
        return _inner()

    monkeypatch.setattr(translation_service, "translate_texts", _fake_errores)
    estado = asyncio.run(job_runner.execute_job(job_id, session_factory=factory))
    assert estado == "failed"

    db = factory()
    assert history_service.get_job(db, job_id).status == "failed"
    db.close()


def test_execute_job_cancelado_mientras_esperaba_en_cola(entorno):
    factory, _ = entorno
    db = factory()
    job = history_service.create_queued_job(
        db, filename="mini.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidcarrera",
    )
    job_id = job.id
    db.close()
    # Carrera: el endpoint marcó la cancelación justo cuando el worker lo tomaba
    job_runner.mark_cancel("uuidcarrera")
    estado = asyncio.run(job_runner.execute_job(job_id, session_factory=factory))
    assert estado == "cancelled"


# ── Cancelación vía endpoint ─────────────────────────────────────────────

def test_cancel_endpoint_job_encolado(entorno):
    factory, _ = entorno
    db = factory()
    history_service.create_queued_job(
        db, filename="mini.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidcola",
    )
    db.close()
    client = TestClient(app)
    r = client.post("/jobs/by-uuid/uuidcola/cancel")
    assert r.status_code == 202
    assert r.json()["was"] == "queued"

    db = factory()
    assert history_service.get_job_by_uuid(db, "uuidcola").status == "cancelled"
    db.close()


def test_cancel_endpoint_uuid_inexistente(entorno):
    client = TestClient(app)
    assert client.post("/jobs/by-uuid/nadie/cancel").status_code == 404


# ── Fixes de la revisión adversarial (v3.8) ──────────────────────────────

def test_execute_job_falla_si_revienta_la_persistencia(entorno, monkeypatch):
    """Hallazgo x3: una excepción DESPUÉS de traducir (p.ej. bulk insert
    con la BBDD bloqueada) dejaba el job 'running' zombi para siempre."""
    factory, _ = entorno
    db = factory()
    job = history_service.create_queued_job(
        db, filename="mini.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidzombi",
    )
    job_id = job.id
    db.close()
    job_runner.upload_path("uuidzombi").write_text(SRT_MINI, encoding="utf-8")
    monkeypatch.setattr(translation_service, "translate_texts", _fake_translate_ok)

    def _revienta(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(history_service, "complete_job", _revienta)
    estado = asyncio.run(job_runner.execute_job(job_id, session_factory=factory))
    assert estado == "failed"

    db = factory()
    job = history_service.get_job(db, job_id)
    assert job.status == "failed"  # NUNCA 'running'
    assert "locked" in job.error
    db.close()


def test_srt_con_indices_duplicados_se_renumera(entorno):
    """Hallazgo: índices None/duplicados colapsaban el dict del pipeline
    (traducciones cruzadas). El enqueue normaliza server-side."""
    srt_duplicado = (
        "7\n00:00:01,000 --> 00:00:02,000\nUno.\n\n"
        "7\n00:00:03,000 --> 00:00:04,000\nDos.\n\n"
        "8\n00:00:05,000 --> 00:00:06,000\nTres.\n"
    )
    client = TestClient(app)
    r = client.post(
        "/translate",
        files={"file": ("dup.srt", srt_duplicado.encode("utf-8"), "text/plain")},
        data={"target_lang": "es", "cpl": "38", "job_uuid": "uuiddup"},
    )
    assert r.status_code == 202
    _, tmp_path = entorno
    guardado = (tmp_path / "uploads" / "uuiddup.srt").read_text(encoding="utf-8")
    from app.services import srt_service
    indices = [s.index for s in srt_service.parse_srt(guardado)]
    assert indices == [1, 2, 3]  # renumerado, sin colisiones


def test_translate_rechaza_uuid_con_job_activo(entorno):
    """Hallazgo: dos jobs con el mismo uuid compartían el archivo de
    uploads (el 2º sobrescribía al 1º) y la cancelación era ambigua."""
    client = TestClient(app)
    datos = {"target_lang": "es", "cpl": "38", "job_uuid": "uuidrepe"}
    r1 = client.post(
        "/translate",
        files={"file": ("a.srt", SRT_MINI.encode("utf-8"), "text/plain")},
        data=datos,
    )
    assert r1.status_code == 202
    r2 = client.post(
        "/translate",
        files={"file": ("b.srt", SRT_MINI.encode("utf-8"), "text/plain")},
        data=datos,
    )
    assert r2.status_code == 409


def test_delete_rechaza_job_vivo(entorno):
    """Hallazgo: 'borrar todo' podía borrar la fila de la película en
    curso mientras el worker seguía gastando OpenAI en ella."""
    factory, _ = entorno
    db = factory()
    job = history_service.create_queued_job(
        db, filename="viva.srt", target_lang="es", cpl=38,
        context="", job_uuid="uuidviva",
    )
    job_id = job.id
    db.close()
    client = TestClient(app)
    assert client.delete(f"/jobs/{job_id}").status_code == 409
    # Cancelado ya se puede borrar
    client.post("/jobs/by-uuid/uuidviva/cancel")
    assert client.delete(f"/jobs/{job_id}").status_code == 204


def test_migracion_repara_backfill_interrumpido(tmp_path):
    """Hallazgo: pysqlite autocommitea DDL — si el proceso moría entre
    los ALTER y el backfill, n_cues quedaba a 0 para siempre. Ahora el
    backfill es incondicional e idempotente en cada arranque."""
    import sqlite3

    from sqlalchemy import create_engine, text

    from app.core.database import _ensure_schema

    db_path = tmp_path / "migra.db"
    con = sqlite3.connect(db_path)
    # Esquema v3.7 (sin columnas nuevas) + un job completado con 2 cues
    con.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, filename VARCHAR(512) NOT NULL,
            target_lang VARCHAR(16) NOT NULL, cpl INTEGER NOT NULL,
            context TEXT NOT NULL, started_at DATETIME NOT NULL,
            elapsed_s FLOAT NOT NULL, status VARCHAR(16) NOT NULL,
            cpl_compliance FLOAT NOT NULL, tokens_prompt INTEGER NOT NULL,
            tokens_completion INTEGER NOT NULL, error TEXT NOT NULL
        );
        CREATE TABLE translations (
            id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
            cue_idx INTEGER NOT NULL, source_text TEXT NOT NULL,
            target_text TEXT NOT NULL, created_at DATETIME NOT NULL
        );
        INSERT INTO jobs VALUES (1,'a.srt','es',38,'','2026-01-01',1.0,
            'completed',99.0,10,10,'');
        INSERT INTO translations VALUES (1,1,1,'Hi','Hola','2026-01-01');
        INSERT INTO translations VALUES (2,1,2,'Bye','Adiós','2026-01-01');
    """)
    con.commit()
    con.close()

    eng = create_engine(f"sqlite:///{db_path}", future=True)
    _ensure_schema(eng)  # primera pasada: columnas + backfill + índice

    # Simular la interrupción histórica: columnas presentes, backfill no
    with eng.begin() as c:
        c.execute(text("UPDATE jobs SET n_cues = 0"))
    _ensure_schema(eng)  # segundo arranque: debe auto-reparar

    with eng.connect() as c:
        assert c.execute(text("SELECT n_cues FROM jobs WHERE id=1")).scalar() == 2
        indices = [r[1] for r in c.execute(text("PRAGMA index_list(jobs)"))]
        assert "ix_jobs_job_uuid" in indices


# ── Recovery al arrancar ─────────────────────────────────────────────────

def test_recovery_marca_zombis_y_conserva_la_cola(entorno):
    factory, _ = entorno
    db = factory()
    encolado = history_service.create_queued_job(
        db, filename="a.srt", target_lang="es", cpl=38,
        context="", job_uuid="qqq111",
    )
    zombi = history_service.create_queued_job(
        db, filename="b.srt", target_lang="es", cpl=38,
        context="", job_uuid="rrr222",
    )
    history_service.mark_running(db, zombi)

    n = history_service.recover_interrupted_jobs(db)
    assert n == 1
    assert history_service.get_job(db, zombi.id).status == "failed"
    # El encolado sobrevive al reinicio: el worker lo retomará
    assert history_service.get_job(db, encolado.id).status == "queued"
    assert history_service.next_queued_job_id(db) == encolado.id
    db.close()
