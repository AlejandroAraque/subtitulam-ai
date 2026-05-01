"""
Servicio de auto-contexto a partir del nombre de archivo.

Hace una llamada previa a GPT-4o-mini con el título de la obra para
obtener un bloque de contexto que se inyecta luego como `context` en
la traducción real. Pensado como complemento (NO sustituto) del campo
`context` que el usuario puede rellenar manualmente.

Mitigación de hallucinación:
  - Modelo barato (gpt-4o-mini) y temperatura baja.
  - El system prompt ordena "CONTEXTO NO DISPONIBLE" si la obra es
    desconocida — devolvemos string vacío en ese caso.
  - Logging del título limpio + del contexto generado para auditoría.
"""
from __future__ import annotations

import logging
import re
import time

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
client = AsyncOpenAI()


logger = logging.getLogger("subtitulam.context")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


CONTEXT_MODEL = "gpt-4o-mini"   # ~60× más barato que gpt-4o para esta tarea
NO_CONTEXT_MARKER = "CONTEXTO NO DISPONIBLE"


# ── Limpieza del título ──────────────────────────────────────────────────────

_EXT_RE       = re.compile(r"\.(srt|ass|sub|vtt)$", re.IGNORECASE)
_SEP_RE       = re.compile(r"[_.\-]+")
_BRACKETS_RE  = re.compile(r"\[[^\]]*\]")           # [BluRay], [1080p], etc.
_MULTI_SPACE  = re.compile(r"\s{2,}")


def clean_title(filename: str) -> str:
    """Convierte un nombre de archivo a un título legible para el LLM.

    Ejemplos:
      'BreakingBad_S01E01.srt'      → 'Breaking Bad S01E01'
      'jakobs.ross.2024.srt'        → 'jakobs ross 2024'
      'OPPENHEIMER [BluRay].srt'    → 'OPPENHEIMER'
    """
    name = _EXT_RE.sub("", filename)
    name = _BRACKETS_RE.sub(" ", name)
    name = _SEP_RE.sub(" ", name)
    name = _MULTI_SPACE.sub(" ", name).strip()
    return name


# ── API pública ──────────────────────────────────────────────────────────────

async def generate_context_from_title(filename: str) -> str:
    """Pre-llamada a GPT-4o-mini con el título para extraer contexto.

    Returns:
        - String con el contexto (2-3 frases) si el LLM reconoce la obra.
        - String vacío "" si la obra es desconocida o si la llamada falla.

    Nunca lanza excepción al caller — un fallo se traduce en "" y un log.
    """
    title = clean_title(filename)
    if not title:
        return ""

    system_msg = (
        "Eres un asistente que provee contexto breve y fiable para traductores "
        "profesionales de subtítulos. Responde en español, en 2-3 frases (máx "
        "80 palabras), incluyendo: género, registro narrativo, personajes "
        "principales si los conoces.\n\n"
        f"REGLA CRÍTICA: si NO conoces la obra con seguridad razonable, "
        f"responde EXACTAMENTE el texto: '{NO_CONTEXT_MARKER}'. "
        "NO inventes personajes ni trama. Es preferible no responder a alucinar."
    )
    user_msg = f"Título: {title}"

    t0 = time.time()
    try:
        response = await client.chat.completions.create(
            model=CONTEXT_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=120,
        )
    except Exception as e:
        logger.warning("auto-context falló para '%s': %s", title, e)
        return ""

    dt = time.time() - t0
    text = (response.choices[0].message.content or "").strip()

    usage = getattr(response, "usage", None)
    if usage:
        logger.info(
            "auto-context · '%s' · %.2fs · tokens=%d/%d",
            title, dt, usage.prompt_tokens, usage.completion_tokens,
        )
    else:
        logger.info("auto-context · '%s' · %.2fs", title, dt)

    if NO_CONTEXT_MARKER in text:
        logger.info("auto-context · '%s' → no reconocido por el LLM", title)
        return ""

    return text
