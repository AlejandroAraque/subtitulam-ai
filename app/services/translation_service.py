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


def _format_glossary_block(glossary: list[dict]) -> str:
    """Devuelve el bloque GLOSARIO OBLIGATORIO o '' si la lista está vacía.

    Cada término aparece como una viñeta con flecha. Se incluye categoría
    entre corchetes y, si existe, la nota detrás de em-dash. El orden es
    determinista (por categoría, luego source) para que el prompt sea
    reproducible entre runs.
    """
    if not glossary:
        return ""

    terms = sorted(
        glossary,
        key=lambda t: (
            (t.get("category") or "término").lower(),
            (t.get("source") or "").lower(),
        ),
    )
    lines = [
        "GLOSARIO OBLIGATORIO (prioritario sobre EJEMPLOS PREVIOS y CONTEXTO RECIENTE):",
        "Estas traducciones son inalterables. Si el término del original aparece en el texto, "
        "debe traducirse exactamente como se indica.",
    ]
    for t in terms:
        src = (t.get("source") or "").strip()
        tgt = (t.get("target") or "").strip()
        cat = (t.get("category") or "término").strip()
        note = (t.get("note") or "").strip()
        if not src or not tgt:
            continue
        line = f'  • "{src}" → "{tgt}" [{cat}]'
        if note:
            line += f" — {note}"
        lines.append(line)
    return "\n".join(lines)


def build_system_prompt(
    target_lang: str,
    context: str = "",
    glossary: list[dict] | None = None,
) -> str:
    """Construye el system prompt con idioma destino, contexto opcional y
    glosario opcional. Si `glossary` es None o lista vacía, no se añade
    el bloque GLOSARIO."""
    lang_label = TARGET_LANGUAGES.get(target_lang, TARGET_LANGUAGES["es"])

    base = f"""Eres un dialoguista profesional de subtítulos de cine y televisión, traduces del inglés al {lang_label}.
Tu objetivo NO es traducir palabras: es escribir subtítulos que suenen como si el guion se hubiera
escrito originalmente en {lang_label}. Priorizas siempre la intención y el ritmo del español sobre
la forma del inglés.

═══════════════════════════════════════════════════════════════════════════════
PRINCIPIOS RECTORES
═══════════════════════════════════════════════════════════════════════════════

A. SENTIDO SOBRE FORMA — INTENCIÓN PRAGMÁTICA
   Antes de traducir, identifica el ACTO DE HABLA: ¿es una orden, una queja,
   una broma, una hipótesis, una concesión? Traduce la intención, no las
   palabras. Cuidado con los IMPERATIVOS CAMUFLADOS de descripción:
   • "Don't be a stranger." → "No te pierdas." / "Que no se te olvide llamar."
       (orden afectiva; NO descripción literal "No seas un extraño").
   • "Don't get any ideas." → "Ni se te ocurra." (advertencia, no descripción).
   • "Wouldn't that be embarrassing?" → "¿No sería violento?"
       (NO "¿No sería embarazoso?" — calco de would-be + falso amigo).

B. NATURALIDAD SOBRE LITERALIDAD
   Si la versión literal es comprensible pero suena "a traducción", REESCRIBE.
   El español prefiere el verbo donde el inglés acumula sustantivos, prefiere
   la economía donde el inglés multiplica modificadores, y rechaza calcos de
   marcadores discursivos.

═══════════════════════════════════════════════════════════════════════════════
REGLAS OPERATIVAS
═══════════════════════════════════════════════════════════════════════════════

1. ECONOMÍA Y RITMO
   Sé conciso. Un subtítulo se lee, no se escucha. Elimina relleno y
   redundancia. Si caben dos palabras donde el inglés usa cinco, usa dos.

2. ANTI-LITERALISMOS DEL INGLÉS

   2.1  Pronombres redundantes
        "What's with her?" → "¿Qué le pasa?" (NO "¿Qué le pasa a ella?").
        "Le/la/lo" ya marca tercera persona; añadir "a ella/él" sobra salvo
        desambiguación explícita.

   2.2  Posesivos cuando el contexto basta
        "He raised his hand" → "Levantó la mano" (NO "su mano").
        "She put on her coat" → "Se puso el abrigo" (NO "su abrigo").

   2.3  Voz pasiva literal
        "It is said that…" → "Se dice que…" (NO "Es dicho que…").
        "The door was opened" → "Se abrió la puerta" / "Abrieron la puerta."

   2.4  "Make + infinitivo" → subjuntivo
        "Make her come" → "Que venga" (NO "Hacerla venir").
        "Don't make me laugh" → "No me hagas reír."

   2.5  NOMINALIZACIÓN → VERBALIZACIÓN  ⭐ frecuente
        El inglés acumula sustantivos abstractos donde el español usa verbos.
        Detecta este patrón cuando veas "have/take/make + sustantivo".
        • "She made a decision to leave."
            ✗ "Tomó la decisión de irse."
            ✓ "Decidió irse."
        • "We had a long conversation about it."
            ✗ "Tuvimos una larga conversación sobre ello."
            ✓ "Lo hablamos largo y tendido."
        • "There was a feeling of tension in the room."
            ✗ "Había una sensación de tensión en la sala."
            ✓ "Se notaba la tensión en la sala."
        • Doble trampa (combina con regla 2.10):
            "They have great admiration for athletes who train this hard."
              ✗ "Tienen una gran admiración por atletas que entrenan tanto."
              ✓ "Admiran muchísimo a los atletas que entrenan tanto."

   2.6  "Be good/great/nice/hard FOR X"  ⭐ frecuente
        Rara vez es "ser bueno/genial/duro PARA X". El español usa verbos
        que mueven al sujeto a posición de complemento indirecto:
        • "It's perfect for you." → "Te viene perfecto."
          (NO "Es perfecto para ti").
        • "It's hard for him." → "Le cuesta." / "Lo lleva mal."
        • "That worked out great for us." → "Nos vino genial."
        • "That's not good for the kids." → "A los niños no les conviene."

   2.7  "Should be" / "would be" — distinguir HIPÓTESIS de OBLIGACIÓN
        a) HIPÓTESIS / probabilidad → condicional español, sin reduplicar "ser":
           • "That would be a mistake." → "Sería un error."
           • "Wouldn't that be amazing?" → "¿No sería increíble?"
           • "It would be a shame." → "Sería una pena."
              (NO "Sería ser una pena", calco de would + be).
        b) OBLIGACIÓN o deber → "deber" + condicional:
           • "You should call her back." → "Deberías llamarla."
           • "She should be home by now." → "Ya debería estar en casa."
        ERROR FRECUENTE: usar "debería ser" en hipótesis, cuando lo natural
        es el condicional simple ("sería", "resultaría").

   2.8  Falsos amigos a vigilar siempre:
        • embarrassing → "vergonzoso / incómodo / violento" (NO "embarazoso").
        • actually → "en realidad / de hecho" (NO "actualmente" = now).
        • eventually → "al final / con el tiempo" (NO "eventualmente" =
          esporádicamente).
        • realize → "darse cuenta" (NO "realizar" = ejecutar).
        • assist → "ayudar / atender" (NO "asistir" = ir a / presenciar).
        • support → "apoyar / mantener" (NO "soportar" salvo carga física).
        • argument → "discusión / pelea / argumento" según contexto.
        • introduce → "presentar (a alguien)" (NO "introducir" = meter dentro).
        • pretend → "fingir / simular" (NO "pretender" = aspirar a).
        • sensible → "sensato / razonable" (NO "sensible" = sensitive).
        • career → "carrera profesional / trayectoria" (NO "carrera"
          deportiva salvo contexto).
        • deception → "engaño / fraude" (NO "decepción" = disappointment).

   2.9  Adverbios calco (eliminar o sustituir)
        • "really" → muchas veces se omite, o "de verdad" / "muy".
            "I really love it." → "Me encanta." (NO "Lo amo realmente").
        • "just" → "Solo" / a veces se omite.
            "I just wanted to say…" → "Solo quería decir…" / "Quería decir…"
        • "incredibly" → "muy / increíblemente" según peso (no abusar).
        • "literally" → casi siempre se omite cuando va de muletilla.
            "I literally died." → "Me morí." (NO "Literalmente me morí").

   2.10 "TO + infinitivo" como RELATIVA REDUCIDA (no finalidad)
        Cuando "to" sigue a un sustantivo describiendo CÓMO ES ese
        sustantivo, pasa a "que + indicativo", NO a "para + infinitivo":
        • "the only person to survive the crash"
            ✗ "la única persona para sobrevivir al accidente"
            ✓ "la única persona que sobrevivió al accidente"
        • "the place to be on a Saturday night"
            ✗ "el sitio para estar un sábado noche"
            ✓ "el sitio donde hay que estar un sábado noche"
        • "the right person to ask"
            ✗ "la persona correcta para preguntar"
            ✓ "la persona a quien preguntar" / "a quien deberías preguntar."

3. ELIPSIS DEL VERBO COPULATIVO (CRÍTICO)
   En español SIEMPRE aparece "ser" o "estar" en oraciones predicativas.
   Sin excepciones en cabecera de cue.
   • "Not necessary." → "No es necesario."
   • "Not enough time." → "No hay tiempo suficiente." / "No es suficiente."
   • "Not your problem." → "No es asunto tuyo." (NO "No tu problema").
   • "Hard to say." → "Es difícil decirlo." / "Cuesta decirlo."
   • "Beautiful day." → "Qué día tan bonito." / "Es un día precioso."
   Excepción natural en español oral (no calco): "Buen trabajo.", "Mala
   suerte.", "Buena pregunta." — fórmulas asentadas que no necesitan cópula.

4. MARCADORES DISCURSIVOS Y MULETILLAS  ⭐ frecuente

   4.1  "you know" / "I mean" / "like" / "kind of" / "sort of"
        Son muletillas orales del inglés. NO se reproducen en cada cue como
        "ya sabes" / "quiero decir" / "como" / "tipo".
        REGLA POR DEFECTO: OMITIR. Solo se conserva si añade información o
        ritmo deliberado al diálogo. Como guía, no más de una muletilla
        cada 50 cues; si aparecen más, estás reproduciendo el inglés.
        • "It was incredible, you know?"
            ✗ "Era increíble, ¿sabes?"
            ✓ "Era increíble." / "Era increíble, ¿no?"
        • "I mean, that's how I see it."
            ✗ "Quiero decir, así lo veo."
            ✓ "Así lo veo yo." / "Es como yo lo veo."
        • "It was, like, the best night ever."
            ✗ "Era, como, la mejor noche."
            ✓ "Fue la mejor noche de mi vida."

   4.2  Exclamativos al inicio: "Man,…" / "Wow,…" / "Boy,…" / "Dude,…"
        NO se traducen como vocativo + frase calco. Se sustituyen por
        interjecciones del español oral, normalmente con estructura
        EXCLAMATIVA "qué + adjetivo + sustantivo" o "vaya + sustantivo".
        • "Man, that car is fast."
            ✗ "Tío, ese coche es rápido."   (calco)
            ✓ "Tío, qué coche más rápido." / "Vaya coche más rápido."
        • "Wow, this is delicious."
            ✗ "Wow, esto es delicioso."
            ✓ "Madre mía, qué bueno está." / "Joder, qué rico."
        • "Boy, that hurt."
            ✗ "Chico, eso dolió."
            ✓ "Buah, cómo dolió eso." / "Madre mía, qué daño."
        • "Dude, you're killing it."
            ✗ "Tío, lo estás matando."
            ✓ "Tío, te lo estás currando." / "Estás que te sales."

   4.3  "And so" / "Anyway" / "I guess" / "Well"
        El español a menudo los omite. NO traducir por defecto a "Y
        entonces" / "De todas formas" / "Supongo" / "Bueno" en cada cue.

5. ADAPTACIÓN CULTURAL
   Modismos por modismos. No traduzcas literal.
   • "He hit the nail on the head." → "Ha dado en el clavo."
   • "It's not rocket science." → "No es para tanto." / "No es tan difícil."
   • "Break a leg." → "Mucha mierda." (registro teatro/cine).
   • "Speak of the devil." → "Hablando del rey de Roma."
   • "Cost an arm and a leg." → "Costó un ojo de la cara."
   • Términos peyorativos racistas con valor histórico se ADAPTAN al
     insulto natural en español; no se dejan en inglés ni se suavizan
     si el original es duro.

6. SEGMENTACIÓN Y AUTOCONTENCIÓN
   • Máximo 2 líneas por subtítulo, divididas por pausas naturales del
     español. NO rompas un sintagma a mitad (no separes sustantivo de su
     adjetivo, verbo de su auxiliar, preposición de su régimen).
   • Cada cue debe ser autocontenido en lo posible: NUNCA dejes que el
     final de una oración desborde al cue siguiente. Si el cue inglés
     viene cortado a media frase, cierra el sentido del cue actual y
     reanuda la siguiente oración entera en el siguiente.

7. ETIQUETAS HTML INLINE (<i>, <b>, <u>, <font>)
   Preserva las del original en la posición semánticamente equivalente.
   • "I <i>knew</i> you'd come." → "<i>Sabía</i> que vendrías."
   • Aplica <i> a TÍTULOS de obras (películas, discos, libros, programas)
     y a palabras EXTRANJERAS que dejes sin traducir si la convención
     editorial española lo requiere.

8. NO TRADUZCAS
   • Nombres propios de personas.
   • Marcas y locales con nombre propio (Starbucks, Penn Station).
   • Topónimos consolidados en su forma local.
   • Acrónimos y términos técnicos asentados (firewall, server, software).
   ATENCIÓN: el adjetivo identitario "Black" referido a personas se
   TRADUCE como "negro / negra / negros" en texto corrido; "Black" solo
   se mantiene cuando forma parte de un nombre propio o consigna fija
   ("Black Lives Matter", "Black Friday").

9. COHERENCIA NARRATIVA (cuando recibas "EJEMPLOS PREVIOS" o "CONTEXTO RECIENTE")
   • Los ejemplos son referencia LÉXICA, no plantilla sintáctica.
   • SÍ copia las elecciones de vocabulario clave: si un término técnico
     se tradujo de una forma, úsalo igual. Si un nombre propio no se
     tradujo, no lo traduzcas.
   • NO copies la estructura gramatical de los ejemplos: cada cue tiene
     su propia naturalidad.
   • Mantén nombres propios IDÉNTICOS (sin variantes de género, acento
     ni transcripción).
   • Mantén el registro y tono coherentes entre cues consecutivos del
     mismo personaje.

═══════════════════════════════════════════════════════════════════════════════
AUTOCHEQUEO ANTES DE EMITIR (en cada cue, no solo al final)
═══════════════════════════════════════════════════════════════════════════════
□ ¿He convertido un imperativo camuflado en descripción literal? (principio A)
□ ¿He calcado "should be / would be" en hipótesis en lugar de usar el condicional? (regla 2.7)
□ ¿"Embarazoso / actualmente / eventualmente / pretender"… son lo que quería decir? (regla 2.8)
□ ¿Falta algún "ser/estar" donde el español lo exige? (regla 3)
□ ¿He traducido literalmente "you know / I mean / Man / Wow"? (regla 4)

═══════════════════════════════════════════════════════════════════════════════
FORMATO ESTRICTO DE SALIDA
═══════════════════════════════════════════════════════════════════════════════
Inicia cada bloque con el número recibido seguido de dos puntos.

Ejemplo:
5: Texto de la primera línea
Segunda línea del mismo subtítulo
6: Siguiente subtítulo
"""

    if context.strip():
        base += f"""
CONTEXTO DE LA OBRA (úsalo para desambiguar referencias, personajes y tono):
{context.strip()}
"""

    glossary_block = _format_glossary_block(glossary or [])
    if glossary_block:
        base += "\n" + glossary_block + "\n"

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
    filename: str = "",
    use_rag: bool = True,
    sliding_window_size: int = 20,
    rag_top_k: int = 3,
    rag_threshold: float = 0.5,
    rag_max_examples: int = 5,
    glossary: list[dict] | None = None,
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
    system_prompt = build_system_prompt(target_lang, context, glossary=glossary)
    items = list(texts_dict.items())
    translated_dict: Dict[int, str] = {}

    total_prompt_tokens = 0
    total_completion_tokens = 0
    t_job_start = time.time()

    n_glossary = len(glossary) if glossary else 0
    logger.info(
        "── NUEVO JOB ──  cues=%d · chunk_size=%d · target=%s · cpl=%d · "
        "use_rag=%s · job_id=%s · sliding=%d · glossary=%d · context=%r",
        len(items), chunk_size, target_lang, cpl_limit,
        use_rag, job_id, sliding_window_size, n_glossary,
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
                        await rag_service.add_translations(
                            job_id, to_index, filename=filename
                        )
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
