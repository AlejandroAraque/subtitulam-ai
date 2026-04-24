import os
import re
import time
import logging
from typing import Dict
from openai import AsyncOpenAI
from dotenv import load_dotenv
from app.utils.text_utils import ajustar_cpl_optimo

load_dotenv()
client = AsyncOpenAI()

# ── Logger configurado para trazar cada batch ───────────────────────────────
logger = logging.getLogger("subtitulam.translation")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ── Variedades de español + otros idiomas destino ───────────────────────────
TARGET_LANGUAGES = {
    "es":     "español de España (es-ES)",
    "es-ES":  "español de España (es-ES)",
    "es-419": "español neutro de Latinoamérica (es-419)",
    "fr":     "francés (fr-FR)",
    "de":     "alemán (de-DE)",
    "pt":     "portugués de Brasil (pt-BR)",
    "it":     "italiano (it-IT)",
}


def build_system_prompt(target_lang: str, context: str = "") -> str:
    """Construye el system prompt con idioma destino y contexto opcional."""
    lang_label = TARGET_LANGUAGES.get(target_lang, TARGET_LANGUAGES["es"])

    base = f"""Eres un traductor profesional experto en subtítulos de cine y televisión del inglés al {lang_label}.
Tu objetivo es producir subtítulos naturales, concisos y fáciles de leer.

INSTRUCCIONES OBLIGATORIAS:
1. Economía del lenguaje: Sé conciso, elimina relleno.
2. Naturalidad: "I guess we should get going" → "Supongo que deberíamos ir tirando".
3. Adaptación cultural: No traduzcas literal. "He hit the nail on the head" → "Ha dado en el clavo".
4. Segmentación: Máximo 2 líneas, divididas por pausas naturales.
5. Elipsis del verbo copulativo (CRÍTICO): En español SIEMPRE debe aparecer el verbo "ser" o "estar". "Not necessary" → "No es necesario".
6. PRESERVA las etiquetas HTML inline del original (<i>, <b>, <u>, <font>) en la posición semánticamente equivalente.
   - "I <i>knew</i> you'd come through" → "<i>Sabía</i> que no me fallarías".
   - Si la etiqueta envuelve una palabra con énfasis, tradúcela manteniendo el énfasis donde cae naturalmente en destino.
7. NO TRADUZCAS: nombres propios de personas, marcas comerciales, topónimos consolidados (Starbucks, Penn Station, Mr. O'Brien) ni acrónimos técnicos (firewall, server).
8. Formato ESTRICTO de salida:
   - Inicia cada bloque con el número recibido seguido de dos puntos.
   - Ejemplo:
     5: Texto de la primera línea
     Segunda línea del mismo subtítulo
     6: Siguiente subtítulo
"""

    if context.strip():
        base += f"""
CONTEXTO DE LA OBRA (úsalo para desambiguar referencias, personajes y tono):
{context.strip()}
"""
    return base


def parsear_traducciones(traduccion_bruta: str) -> Dict[int, str]:
    """Extrae el texto traducido y lo asocia a su índice original."""
    traducciones: Dict[int, str] = {}
    idx_actual = None

    for linea in traduccion_bruta.splitlines():
        m = re.match(r'^\s*(\d+)\s*:\s*(.*)$', linea)
        if m:
            idx_actual = int(m.group(1))
            traducciones[idx_actual] = m.group(2).strip()
        else:
            if idx_actual is not None and linea.strip():
                traducciones[idx_actual] += "\n" + linea.strip()

    return traducciones


async def translate_texts(
    texts_dict: Dict[int, str],
    chunk_size: int = 5,
    target_lang: str = "es",
    context: str = "",
    cpl_limit: int = 38,
) -> Dict[int, str]:
    """
    Recibe {index: "texto original"} y devuelve {index: "texto traducido ajustado a CPL"}.
    Trazas de cada batch escritas en el logger para auditoría.
    """
    system_prompt = build_system_prompt(target_lang, context)
    items = list(texts_dict.items())
    translated_dict: Dict[int, str] = {}

    total_prompt_tokens = 0
    total_completion_tokens = 0
    t_job_start = time.time()

    logger.info(
        "── NUEVO JOB ──  cues=%d · chunk_size=%d · target=%s · cpl=%d · context=%r",
        len(items), chunk_size, target_lang, cpl_limit,
        (context[:80] + "…") if len(context) > 80 else context,
    )

    for i in range(0, len(items), chunk_size):
        bloque = items[i:i + chunk_size]
        texto_prompt = "\n\n".join([f"{idx}: {texto}" for idx, texto in bloque])
        batch_label = f"batch {bloque[0][0]}-{bloque[-1][0]}"

        logger.info("→ %s · enviando (%d cues)", batch_label, len(bloque))
        t_batch_start = time.time()

        try:
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": texto_prompt},
                ],
                temperature=0.3,
                max_tokens=800,
            )

            dt = time.time() - t_batch_start
            usage = getattr(response, "usage", None)
            if usage:
                total_prompt_tokens += usage.prompt_tokens
                total_completion_tokens += usage.completion_tokens
                logger.info(
                    "← %s · %.2fs · tokens prompt=%d completion=%d",
                    batch_label, dt, usage.prompt_tokens, usage.completion_tokens,
                )
            else:
                logger.info("← %s · %.2fs", batch_label, dt)

            traduccion_bruta = response.choices[0].message.content.strip()
            traducciones_parseadas = parsear_traducciones(traduccion_bruta)

            for idx, texto_trad in traducciones_parseadas.items():
                translated_dict[idx] = ajustar_cpl_optimo(texto_trad, max_cpl=cpl_limit)

        except Exception as e:
            logger.error("✖ %s · fallo: %s", batch_label, str(e))
            for idx, texto in bloque:
                translated_dict[idx] = f"[ERROR] {texto}"

    dt_total = time.time() - t_job_start
    logger.info(
        "── JOB COMPLETO ──  %.2fs · tokens total prompt=%d completion=%d",
        dt_total, total_prompt_tokens, total_completion_tokens,
    )

    return translated_dict
