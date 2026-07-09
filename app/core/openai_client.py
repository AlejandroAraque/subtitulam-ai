"""Cliente OpenAI compartido, instanciado lazy.

Por qué lazy y no a nivel de módulo: `AsyncOpenAI()` lanza OpenAIError en
el constructor si falta OPENAI_API_KEY. Instanciarlo como side effect de
import hacía que el backend entero no arrancara sin key — incluidos los
endpoints que no usan OpenAI (GET /glossary, GET /jobs) — e impedía
importar los services en tests sin una key real.

Un único cliente para todos los services además comparte el pool HTTP
(antes había 3 pools separados).
"""
from functools import lru_cache

from openai import AsyncOpenAI

import app.core.config  # noqa: F401 — fuerza load_dotenv() antes del cliente


@lru_cache(maxsize=1)
def get_openai() -> AsyncOpenAI:
    """Devuelve el cliente AsyncOpenAI singleton (creado en el primer uso).

    max_retries=5: el SDK reintenta 429/5xx con backoff exponencial
    respetando Retry-After. Con el default (2), una ráfaga de rate limit
    marcaba chunks [ERROR] en cascada y al superar el 20% el job entero
    moría con los tokens ya pagados.
    """
    return AsyncOpenAI(max_retries=5)
