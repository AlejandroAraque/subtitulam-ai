"""
Servicio de embeddings — wrapper sobre OpenAI text-embedding-3-small.

Convierte texto a vector de 1536 dimensiones, útil para:
  - indexar Translations en ChromaDB (v2.2)
  - lanzar queries de retrieval semántico (v2.3)

Modelo elegido: text-embedding-3-small
  · 1536 dims (vs 3072 de large)
  · $0.02 / 1M tokens — 6.5x más barato que large
  · calidad casi idéntica para inglés común
"""
from __future__ import annotations

import logging
import time
from typing import List

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
client = AsyncOpenAI()


# ── Logger consistente con translation_service ──────────────────────────────
logger = logging.getLogger("subtitulam.embeddings")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ── Constantes ──────────────────────────────────────────────────────────────
EMBED_MODEL: str = "text-embedding-3-small"
EMBED_DIM:   int = 1536


# ── API pública ─────────────────────────────────────────────────────────────

async def embed_one(text: str) -> List[float]:
    """Devuelve el embedding de un único texto.

    Raises:
        ValueError si el texto está vacío.
    """
    if not text or not text.strip():
        raise ValueError("embed_one: texto vacío.")

    t0 = time.time()
    response = await client.embeddings.create(
        model=EMBED_MODEL,
        input=[text],
    )
    dt = time.time() - t0

    vec = response.data[0].embedding
    tokens = response.usage.total_tokens if response.usage else 0
    logger.info("embed_one · 1 texto · %.2fs · tokens=%d", dt, tokens)
    return vec


async def embed_batch(texts: List[str]) -> List[List[float]]:
    """Devuelve embeddings de N textos en una sola llamada.

    Optimización: una llamada con lista es ~10x más rápida y barata que N
    llamadas individuales (latencia HTTP amortizada, mejor packing).

    Returns:
        Lista de N vectores, alineada 1:1 con la entrada.

    Raises:
        ValueError si la entrada está vacía o contiene strings vacíos.
    """
    if not texts:
        raise ValueError("embed_batch: lista de textos vacía.")
    if any(not t or not t.strip() for t in texts):
        raise ValueError("embed_batch: hay textos vacíos en la lista.")

    t0 = time.time()
    response = await client.embeddings.create(
        model=EMBED_MODEL,
        input=texts,
    )
    dt = time.time() - t0

    # OpenAI garantiza orden: response.data[i] corresponde a texts[i]
    vectors = [item.embedding for item in response.data]
    tokens  = response.usage.total_tokens if response.usage else 0
    logger.info("embed_batch · %d textos · %.2fs · tokens=%d", len(texts), dt, tokens)
    return vectors
