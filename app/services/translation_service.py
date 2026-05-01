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
8. COHERENCIA NARRATIVA (si el usuario incluye los bloques opcionales más abajo):
   - "EJEMPLOS PREVIOS" o "CONTEXTO RECIENTE" muestran traducciones ya hechas.
   - Mantén las MISMAS elecciones léxicas para términos repetidos (si "bank"
     se tradujo como "banco" antes, no uses "orilla" ahora).
   - Mantén nombres propios IDÉNTICOS (sin variantes de género/acento).
   - Mantén el mismo registro y tono entre cues consecutivos del mismo personaje.
9. Formato ESTRICTO de salida:
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


def build_user_prompt(
    batch_items: list[tuple[int, str]],
    rag_examples: list[dict] | None = None,
    recent_window: list[tuple[int, str, str]] | None = None,
) -> str:
    """Construye el user message del batch con bloques opcionales.

    Args:
        batch_items: lista de (cue_idx, source_text) a traducir AHORA.
        rag_examples: ejemplos retrievados de ChromaDB. Cada dict con keys
            'source_text' y 'target_text'. Si None o vacío, no se incluye
            el bloque.
        recent_window: traducciones recientes del MISMO archivo, en orden
            cronológico. Lista de (cue_idx, source, target). Si None o
            vacío, no se incluye el bloque.

    Returns:
        El texto del user message, listo para mandar a OpenAI.
    """
    parts: list[str] = []

    if rag_examples:
        parts.append("EJEMPLOS PREVIOS (traducciones similares de archivos pasados):")
        for ex in rag_examples:
            src = ex.get("source_text", "").replace("\n", " ").strip()
            tgt = ex.get("target_text", "").replace("\n", " ").strip()
            if src and tgt:
                parts.append(f'  EN: "{src}"')
                parts.append(f'  ES: "{tgt}"')
        parts.append("")

    if recent_window:
        parts.append("CONTEXTO RECIENTE (cues anteriores del MISMO archivo):")
        for cue_idx, src, tgt in recent_window:
            src = src.replace("\n", " ").strip()
            tgt = tgt.replace("\n", " ").strip()
            parts.append(f'  {cue_idx} EN: "{src}"')
            parts.append(f'  {cue_idx} ES: "{tgt}"')
        parts.append("")

    parts.append("AHORA TRADUCE (manteniendo coherencia con lo anterior si aplica):")
    for idx, src in batch_items:
        parts.append(f"{idx}: {src}")

    return "\n".join(parts)


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


async def _retrieve_for_batch(
    batch_items: list[tuple[int, str]],
    k: int,
    threshold: float,
    max_total: int,
) -> list[dict]:
    """RAG: query ChromaDB para cada cue del batch, dedup por source_text,
    devuelve los top max_total ordenados por similitud descendente."""
    from app.services import rag_service  # import local: rompe ciclo
    seen: set[str] = set()
    results: list[dict] = []
    for _, src in batch_items:
        try:
            hits = await rag_service.query_similar(src, k=k)
        except Exception as e:
            logger.warning("RAG query falló para cue: %s", e)
            continue
        for hit in hits:
            if hit["similarity"] < threshold:
                continue
            key = hit["source_text"]
            if key in seen:
                continue
            seen.add(key)
            results.append(hit)
    results.sort(key=lambda h: h["similarity"], reverse=True)
    return results[:max_total]


async def translate_texts(
    texts_dict: Dict[int, str],
    chunk_size: int = 5,
    target_lang: str = "es",
    context: str = "",
    cpl_limit: int = 38,
    *,
    job_id: int | None = None,
    use_rag: bool = True,
    sliding_window_size: int = 20,
    rag_top_k: int = 3,
    rag_threshold: float = 0.5,
    rag_max_examples: int = 5,
) -> dict:
    """
    Recibe {index: "texto original"} y devuelve un dict con:
        - translations: {index: "texto traducido ajustado a CPL"}
        - tokens_prompt: int
        - tokens_completion: int
        - elapsed_s: float

    Modos (ver matriz en docs):
      - use_rag=False                → modo legacy / baseline puro.
      - use_rag=True, job_id=None    → query-only (eval +RAG sin contaminar corpus).
      - use_rag=True, job_id=N       → full: query + indexa cada batch.

    sliding_window_size: cuántas cues anteriores DEL MISMO job inyectar como
        contexto en el prompt. 0 = desactivado (eval sobre samples sueltos).
    """
    system_prompt = build_system_prompt(target_lang, context)
    items = list(texts_dict.items())
    translated_dict: Dict[int, str] = {}

    total_prompt_tokens = 0
    total_completion_tokens = 0
    t_job_start = time.time()

    logger.info(
        "── NUEVO JOB ──  cues=%d · chunk_size=%d · target=%s · cpl=%d · "
        "use_rag=%s · job_id=%s · sliding=%d · context=%r",
        len(items), chunk_size, target_lang, cpl_limit,
        use_rag, job_id, sliding_window_size,
        (context[:60] + "…") if len(context) > 60 else context,
    )

    for i in range(0, len(items), chunk_size):
        bloque = items[i:i + chunk_size]
        batch_label = f"batch {bloque[0][0]}-{bloque[-1][0]}"

        # ── 1. RAG retrieval ──────────────────────────────────────────────
        rag_examples: list[dict] = []
        if use_rag:
            rag_examples = await _retrieve_for_batch(
                bloque,
                k=rag_top_k,
                threshold=rag_threshold,
                max_total=rag_max_examples,
            )

        # ── 2. Sliding window: últimas N traducciones del MISMO job ───────
        recent_window: list[tuple[int, str, str]] = []
        if sliding_window_size > 0 and translated_dict:
            recent_idxs = list(translated_dict.keys())[-sliding_window_size:]
            recent_window = [
                (idx, texts_dict[idx], translated_dict[idx])
                for idx in recent_idxs
            ]

        # ── 3. Construir user prompt con bloques opcionales ───────────────
        user_prompt = build_user_prompt(
            batch_items=bloque,
            rag_examples=rag_examples,
            recent_window=recent_window,
        )

        logger.info(
            "→ %s · %d cues · %d RAG · %d window",
            batch_label, len(bloque), len(rag_examples), len(recent_window),
        )
        t_batch_start = time.time()

        try:
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
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

            # ── 4. Indexar este batch en ChromaDB (solo si full mode) ─────
            if use_rag and job_id is not None:
                from app.services import rag_service  # import local
                to_index = [
                    {
                        "cue_idx":     idx,
                        "source_text": texts_dict[idx],
                        "target_text": translated_dict[idx],
                        "target_lang": target_lang,
                    }
                    for idx, _ in bloque
                    if idx in translated_dict
                       and not translated_dict[idx].startswith("[ERROR]")
                ]
                if to_index:
                    try:
                        await rag_service.add_translations(job_id, to_index)
                    except Exception as e:
                        logger.warning(
                            "RAG indexación falló en %s (no bloqueante): %s",
                            batch_label, e,
                        )

        except Exception as e:
            logger.error("✖ %s · fallo: %s", batch_label, str(e))
            for idx, texto in bloque:
                translated_dict[idx] = f"[ERROR] {texto}"

    dt_total = time.time() - t_job_start
    logger.info(
        "── JOB COMPLETO ──  %.2fs · tokens total prompt=%d completion=%d",
        dt_total, total_prompt_tokens, total_completion_tokens,
    )

    return {
        "translations":      translated_dict,
        "tokens_prompt":     total_prompt_tokens,
        "tokens_completion": total_completion_tokens,
        "elapsed_s":         dt_total,
    }
