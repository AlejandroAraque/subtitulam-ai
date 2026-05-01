"""
Servicio RAG — wrapper sobre ChromaDB para almacenar y consultar
embeddings de Translations.

Capa de abstracción sobre el cliente ChromaDB que permite al resto del
sistema (history_service, runner de evaluación, futuro retrieval en
/translate) trabajar con conceptos del dominio (Job, Translation) en
lugar de detalles del vector store.

Persistencia: data/chromadb/ (archivo local, similar a SQLite).
Embeddings: vía app.services.embeddings_service (OpenAI).
Métrica: similitud coseno (default de ChromaDB).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import chromadb
from chromadb.config import Settings

from app.core.config import DATA_DIR
from app.services.embeddings_service import embed_batch, embed_one


# ── Configuración ───────────────────────────────────────────────────────────
CHROMA_DIR     = DATA_DIR / "chromadb"
COLLECTION     = "translations"

logger = logging.getLogger("subtitulam.rag")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ── Singleton lazy del cliente y la colección ───────────────────────────────
# ChromaDB recomienda un único cliente por proceso. Se inicializa al primer
# acceso para no pagar el coste de abrir el almacén si nadie lo usa.
_client = None
_collection = None


def get_collection():
    """Devuelve la colección 'translations', creándola si no existe."""
    global _client, _collection
    if _collection is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),  # off telemetría
        )
        _collection = _client.get_or_create_collection(
            name=COLLECTION,
            metadata={"hnsw:space": "cosine"},  # similitud coseno explícita
        )
        logger.info("ChromaDB inicializado · path=%s · collection=%s",
                    CHROMA_DIR, COLLECTION)
    return _collection


# ── API pública ─────────────────────────────────────────────────────────────

async def add_translations(
    job_id: int,
    translations: list[dict[str, Any]],
) -> int:
    """Embebe e indexa una lista de translations de un job.

    Args:
        job_id: id del Job al que pertenecen.
        translations: lista de dicts con al menos:
            - id           (int, id de la fila en SQLite)
            - cue_idx      (int)
            - source_text  (str)
            - target_text  (str)
        Pueden incluir extras (target_lang, etc.) que se guardarán en metadata.

    Returns:
        Número de translations indexadas.

    Idempotencia: si un id ya existe, ChromaDB lo SOBRESCRIBE (upsert).
    """
    if not translations:
        return 0

    sources = [t["source_text"] for t in translations]
    vectors = await embed_batch(sources)

    # ID compuesto estable: no depende del Translation.id de SQLite, así
    # podemos indexar EN MITAD de un job (v2.3) antes de que SQLite haya
    # asignado IDs. Idempotente: re-indexar el mismo (job, cue) sobrescribe.
    ids = [f"job_{job_id}_cue_{t['cue_idx']}" for t in translations]
    metadatas = [
        {
            "job_id":      job_id,
            "cue_idx":     t["cue_idx"],
            "target_text": t["target_text"],
            "target_lang": t.get("target_lang", "es"),
        }
        for t in translations
    ]

    col = get_collection()
    col.upsert(
        ids=ids,
        embeddings=vectors,
        documents=sources,
        metadatas=metadatas,
    )
    logger.info("RAG indexado · job=%d · %d translations", job_id, len(translations))
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

    where = None
    if exclude_job_id is not None:
        where = {"job_id": {"$ne": exclude_job_id}}

    col = get_collection()
    res = col.query(
        query_embeddings=[query_vec],
        n_results=k,
        where=where,
    )

    # ChromaDB devuelve los campos en listas paralelas, una entrada por query.
    # Como solo enviamos 1 query, usamos índice [0] en cada uno.
    if not res["ids"] or not res["ids"][0]:
        return []

    ids       = res["ids"][0]
    documents = res["documents"][0]
    distances = res["distances"][0]
    metadatas = res["metadatas"][0]

    # ChromaDB con cosine devuelve "distance" = 1 - similarity.
    # Convertimos a similarity para que el usuario tenga 1.0 = idéntico.
    return [
        {
            "source_text": doc,
            "target_text": meta.get("target_text", ""),
            "similarity":  round(1.0 - dist, 4),
            "job_id":      meta.get("job_id"),
            "cue_idx":     meta.get("cue_idx"),
        }
        for doc, dist, meta in zip(documents, distances, metadatas)
    ]


# ── Utilidades de mantenimiento ─────────────────────────────────────────────

def count() -> int:
    """Cuántos vectores hay indexados ahora mismo."""
    return get_collection().count()


def clear() -> None:
    """Borra TODA la colección. Solo para tests / reset manual."""
    global _client, _collection
    if _client is None:
        get_collection()
    _client.delete_collection(COLLECTION)
    _collection = None
    logger.warning("ChromaDB · colección '%s' BORRADA", COLLECTION)
