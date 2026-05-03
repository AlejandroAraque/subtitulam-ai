"""
Servicio RAG — wrapper sobre Qdrant para almacenar y consultar embeddings
de Translations.

Migrado de ChromaDB a Qdrant en v3.0 para soportar despliegue
production-grade en servidor (Docker compose con Qdrant standalone) y/o
Qdrant Cloud.

Compatible con ambos modos de despliegue sin tocar código:
  - Self-hosted local (default): QDRANT_URL=http://localhost:6333
  - Self-hosted en docker-compose: QDRANT_URL=http://qdrant:6333
  - Qdrant Cloud: QDRANT_URL=https://xyz.qdrant.io + QDRANT_API_KEY=sk-...

Persistencia: gestionada por el propio Qdrant (volumen Docker o cloud).
Embeddings: vía app.services.embeddings_service (OpenAI 1536-dim).
Métrica: similitud coseno (configurable a futuro).

La API pública (`add_translations`, `query_similar`, `count`, `clear`)
es idéntica a la versión anterior con ChromaDB; el resto del sistema no
necesita cambios.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.services.embeddings_service import embed_batch, embed_one

load_dotenv()


# ── Configuración ───────────────────────────────────────────────────────────
QDRANT_URL     = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None  # None si self-hosted sin auth
COLLECTION     = "translations"
VECTOR_SIZE    = 1536  # dimensiones de text-embedding-3-small

# Namespace UUID fijo para generar IDs determinísticos a partir de strings
# como "job_5_cue_42". Garantiza idempotencia: re-indexar el mismo
# (job, cue) sobreescribe el vector porque el UUID resultante es siempre
# el mismo.
_UUID_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")

logger = logging.getLogger("subtitulam.rag")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ── Singleton lazy del cliente y bandera de "colección lista" ───────────────
_client: Optional[QdrantClient] = None
_collection_ready: bool = False


def _make_id(job_id: int, cue_idx: int) -> str:
    """ID determinístico (UUID5) a partir de (job_id, cue_idx).

    Qdrant exige IDs uint64 o UUID; usamos UUID5 con un namespace fijo
    para garantizar idempotencia en upserts.
    """
    return str(uuid.uuid5(_UUID_NAMESPACE, f"job_{job_id}_cue_{cue_idx}"))


def get_client() -> QdrantClient:
    """Devuelve el cliente Qdrant (singleton lazy) con la colección lista."""
    global _client, _collection_ready

    if _client is None:
        kwargs: dict[str, Any] = {"url": QDRANT_URL}
        if QDRANT_API_KEY:
            kwargs["api_key"] = QDRANT_API_KEY
        _client = QdrantClient(**kwargs)
        logger.info(
            "Qdrant cliente inicializado · url=%s · cloud=%s",
            QDRANT_URL, bool(QDRANT_API_KEY),
        )

    if not _collection_ready:
        _ensure_collection()
        _collection_ready = True

    return _client


def _ensure_collection() -> None:
    """Crea la colección si no existe (idempotente)."""
    assert _client is not None
    existing = {c.name for c in _client.get_collections().collections}
    if COLLECTION not in existing:
        _client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )
        logger.info(
            "Qdrant · colección '%s' creada · size=%d · cosine",
            COLLECTION, VECTOR_SIZE,
        )


# ── API pública ─────────────────────────────────────────────────────────────

async def add_translations(
    job_id: int,
    translations: list[dict[str, Any]],
    *,
    filename: str = "",
) -> int:
    """Embebe e indexa una lista de translations de un job.

    Args:
        job_id: id del Job al que pertenecen.
        translations: lista de dicts con al menos:
            - cue_idx      (int)
            - source_text  (str)
            - target_text  (str)
        Pueden incluir extras (target_lang, etc.) que se guardarán en payload.
        filename: nombre del archivo SRT origen. Se guarda en el payload de
            cada vector para permitir filtrado por película/serie en v2.4+.

    Returns:
        Número de translations indexadas.

    Idempotencia: si un id ya existe, Qdrant lo SOBRESCRIBE (upsert) gracias
    al UUID5 determinístico de (job_id, cue_idx).
    """
    if not translations:
        return 0

    sources = [t["source_text"] for t in translations]
    vectors = await embed_batch(sources)

    points = [
        PointStruct(
            id=_make_id(job_id, t["cue_idx"]),
            vector=vec,
            payload={
                "job_id":      job_id,
                "cue_idx":     t["cue_idx"],
                # source_text se guarda EN payload (no como "documents"
                # separados como hacía ChromaDB) para simplificar la query.
                "source_text": t["source_text"],
                "target_text": t["target_text"],
                "target_lang": t.get("target_lang", "es"),
                "filename":    filename or t.get("filename", ""),
            },
        )
        for t, vec in zip(translations, vectors)
    ]

    client = get_client()
    client.upsert(collection_name=COLLECTION, points=points)
    logger.info(
        "RAG indexado · job=%d · %d translations", job_id, len(translations),
    )
    return len(translations)


async def query_similar(
    text: str,
    k: int = 5,
    exclude_job_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Devuelve top-K translations cuyo source_text es similar a `text`.

    Args:
        text: texto en inglés a buscar.
        k: número máximo de resultados.
        exclude_job_id: si se pasa, descarta resultados de ese job
            (útil cuando el caller quiere evitar buscar en el propio job
            que está siendo traducido en este momento).

    Returns:
        Lista de hasta k dicts con shape:
            {
              "source_text": str,    # el cue inglés indexado
              "target_text": str,    # su traducción guardada
              "similarity":  float,  # 0.0 (lejano) a 1.0 (idéntico)
              "job_id":      int,
              "cue_idx":     int,
            }
    """
    query_vec = await embed_one(text)

    query_filter: Optional[Filter] = None
    if exclude_job_id is not None:
        query_filter = Filter(
            must_not=[
                FieldCondition(
                    key="job_id",
                    match=MatchValue(value=exclude_job_id),
                )
            ]
        )

    client = get_client()
    response = client.query_points(
        collection_name=COLLECTION,
        query=query_vec,
        limit=k,
        query_filter=query_filter,
    )

    # query_points devuelve QueryResponse; los hits están en .points como
    # ScoredPoint. Qdrant con cosine devuelve hit.score = similitud directa
    # (rango -1..1, típicamente 0..1 con embeddings normalizados de OpenAI).
    # NO hace falta invertir como hacíamos con ChromaDB (1 - distance).
    return [
        {
            "source_text": hit.payload.get("source_text", ""),
            "target_text": hit.payload.get("target_text", ""),
            "similarity":  round(hit.score, 4),
            "job_id":      hit.payload.get("job_id"),
            "cue_idx":     hit.payload.get("cue_idx"),
        }
        for hit in response.points
    ]


# ── Utilidades de mantenimiento ─────────────────────────────────────────────

def count() -> int:
    """Cuántos vectores hay indexados ahora mismo."""
    client = get_client()
    info = client.get_collection(COLLECTION)
    return info.points_count or 0


def clear() -> None:
    """Borra TODA la colección. Solo para tests / reset manual."""
    global _collection_ready
    client = get_client()
    client.delete_collection(COLLECTION)
    _collection_ready = False  # forzar recreación en el siguiente get_client()
    logger.warning("Qdrant · colección '%s' BORRADA", COLLECTION)
