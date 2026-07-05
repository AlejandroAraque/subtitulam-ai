"""A/B de diseño del RAG: cues vecinas (V1, producción) vs descripción
de escena generada por LLM (V2, hipótesis del usuario).

Pregunta: al inyectar EJEMPLOS PREVIOS en el prompt, ¿desambigua mejor
el contexto crudo (cue anterior) o una descripción destilada de la
escena generada por gpt-4o-mini al indexar?

Diseño:
  - Dos colecciones Qdrant temporales con EL MISMO corpus (jobs previos
    de SQLite, sin cues [ERROR]): `exp_rag_v1` (payload de producción:
    prev/next/context) y `exp_rag_v2` (además, scene_desc por chunk de
    5 cues, generada con gpt-4o-mini).
  - Se traduce CANDY-BAR dos veces (mismo prompt v7.2, mismo glosario),
    una contra cada colección, con la inyección correspondiente
    (monkeypatch de build_user_prompt en V2; producción intacta).
  - Métrica: BLEU/chrF contra la traducción humana profesional
    (eval_against_human) + conteo de hits RAG por run.

Nota de contaminación: el corpus incluye una traducción ANTIGUA de
CANDY (job 7, prompt v6) — esto garantiza retrieval activo y afecta A
AMBAS ramas por igual, así que la comparación V1 vs V2 es válida; los
valores absolutos NO son comparables con el baseline sin-RAG (34.42).

Ejecución (secuencial a propósito — Tier 1 de OpenAI):
    uv run python eval/exp_rag_descripciones.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.core.openai_client import get_openai  # noqa: E402
from app.models.schemas import Job  # noqa: E402
from app.services import rag_service, srt_service, translation_service  # noqa: E402

CANDY_EN = Path(r"C:\Users\AlejandroAraque\Desktop\PERS\peliculas\CANDY\CANDY-BAR_EN-Subtitles.srt")
OUT_DIR = Path(__file__).parent
SLEEP_BETWEEN_RUNS_S = 45  # Tier 1: nunca dos runs pegados


# ─────────────────────────────────────────────────────────────────────
# 1. Corpus desde SQLite (mismo material para ambas colecciones)
# ─────────────────────────────────────────────────────────────────────

def load_corpus() -> list[dict]:
    """Translations de todos los jobs completados, con vecinas y contexto."""
    db = SessionLocal()
    try:
        jobs = list(db.scalars(select(Job).where(Job.status == "completed")).all())
        corpus = []
        for j in jobs:
            by_idx = {t.cue_idx: t.source_text for t in j.translations}
            for t in j.translations:
                if not t.target_text or t.target_text.startswith("[ERROR]"):
                    continue
                corpus.append({
                    "job_id":      j.id,
                    "cue_idx":     t.cue_idx,
                    "source_text": t.source_text,
                    "target_text": t.target_text,
                    "target_lang": j.target_lang,
                    "filename":    j.filename,
                    "prev_text":   by_idx.get(t.cue_idx - 1, ""),
                    "next_text":   by_idx.get(t.cue_idx + 1, ""),
                    "context":     (j.context or "").strip(),
                })
        return corpus
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────
# 2. Descripciones de escena por chunk (solo V2)
# ─────────────────────────────────────────────────────────────────────

async def describe_chunks(corpus: list[dict]) -> dict[tuple, str]:
    """Genera con gpt-4o-mini una descripción de 1 frase por cada chunk
    de 5 cues consecutivas del mismo job. Devuelve {(job_id, chunk_i): desc}.
    """
    from collections import defaultdict
    by_job: dict[int, list[dict]] = defaultdict(list)
    for c in corpus:
        by_job[c["job_id"]].append(c)

    descs: dict[tuple, str] = {}
    client = get_openai()
    n_calls = 0
    for job_id, cues in by_job.items():
        cues.sort(key=lambda c: c["cue_idx"])
        for i in range(0, len(cues), 5):
            chunk = cues[i:i + 5]
            texto = " / ".join(c["source_text"].replace("\n", " ") for c in chunk)
            try:
                r = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": (
                            "Resume en UNA frase corta en español qué ocurre en "
                            "esta secuencia de subtítulos consecutivos de una "
                            "película (quién habla de qué, tono). Solo la frase, "
                            f"sin preámbulos:\n\n{texto}"
                        ),
                    }],
                    temperature=0.2,
                    max_tokens=60,
                )
                desc = (r.choices[0].message.content or "").strip()
            except Exception as e:
                print(f"  [warn] descripción falló (job {job_id} chunk {i//5}): {e}")
                desc = ""
            for c in chunk:
                descs[(c["job_id"], c["cue_idx"])] = desc
            n_calls += 1
    print(f"  {n_calls} descripciones generadas")
    return descs


# ─────────────────────────────────────────────────────────────────────
# 3. Indexado de las dos colecciones temporales
# ─────────────────────────────────────────────────────────────────────

async def build_collection(name: str, corpus: list[dict],
                           descs: dict[tuple, str] | None) -> None:
    from collections import defaultdict
    rag_service.COLLECTION = name
    rag_service._collection_ready = False
    try:
        rag_service.get_client().delete_collection(name)
    except Exception:
        pass
    rag_service._collection_ready = False

    by_job = defaultdict(list)
    for c in corpus:
        item = dict(c)
        if descs is not None:
            item["scene_desc"] = descs.get((c["job_id"], c["cue_idx"]), "")
        by_job[c["job_id"]].append(item)

    total = 0
    for job_id, items in by_job.items():
        total += await rag_service.add_translations(
            job_id, items, filename=items[0]["filename"],
        )
    print(f"  colección {name}: {total} vectores")


# ─────────────────────────────────────────────────────────────────────
# 4. Un run de traducción contra una colección/inyección dada
# ─────────────────────────────────────────────────────────────────────

RAG_HITS = {"n": 0}
_orig_build = translation_service.build_user_prompt


def _prompt_v2(batch_items, rag_examples=None, recent_window=None):
    """Inyección V2: descripción de escena en lugar de la cue vecina."""
    if rag_examples:
        RAG_HITS["n"] += len(rag_examples)
    parts: list[str] = []
    if rag_examples:
        parts.append("EJEMPLOS PREVIOS (traducciones similares de archivos pasados):")
        for ex in rag_examples:
            src = ex.get("source_text", "").replace("\n", " ").strip()
            tgt = ex.get("target_text", "").replace("\n", " ").strip()
            if not (src and tgt):
                continue
            desc = (ex.get("scene_desc") or "").replace("\n", " ").strip()[:120]
            obra = ex.get("context", "").replace("\n", " ").strip()[:100]
            marco = []
            if obra:
                marco.append(f"obra: {obra}")
            if desc:
                marco.append(f"escena: {desc}")
            if marco:
                parts.append(f'  [{" · ".join(marco)}]')
            parts.append(f'  EN: "{src}"')
            parts.append(f'  ES: "{tgt}"')
        parts.append("")
    if recent_window:
        parts.append("CONTEXTO RECIENTE (cues anteriores del MISMO archivo):")
        for cue_idx, src, tgt in recent_window:
            parts.append(f'  {cue_idx} EN: "{src.replace(chr(10), " ").strip()}"')
            parts.append(f'  {cue_idx} ES: "{tgt.replace(chr(10), " ").strip()}"')
        parts.append("")
    parts.append("AHORA TRADUCE (manteniendo coherencia con lo anterior si aplica):")
    for idx, src in batch_items:
        parts.append(f"{idx}: {src}")
    return "\n".join(parts)


def _prompt_v1_contado(batch_items, rag_examples=None, recent_window=None):
    """Inyección V1 (producción) con contador de hits."""
    if rag_examples:
        RAG_HITS["n"] += len(rag_examples)
    return _orig_build(batch_items, rag_examples=rag_examples,
                       recent_window=recent_window)


async def run_variant(tag: str, collection: str, prompt_fn) -> Path:
    print(f"\n=== RUN {tag} (colección {collection}) ===")
    rag_service.COLLECTION = collection
    rag_service._collection_ready = False
    translation_service.build_user_prompt = prompt_fn
    RAG_HITS["n"] = 0

    text = CANDY_EN.read_text(encoding="utf-8-sig")
    cues = srt_service.parse_srt(text)
    cues_src = {s.index: s.content for s in cues}

    result = await translation_service.translate_texts(
        cues_src, target_lang="es", context="", cpl_limit=38,
        job_id=None,           # no indexar: no contaminar el experimento
        filename=CANDY_EN.name,
        use_rag=True, sliding_window_size=20,
        glossary=[],           # glosario OFF: aislar el efecto del RAG
        job_uuid="",
    )
    out = OUT_DIR / f"_exp_candy_{tag}.srt"
    final = srt_service.rebuild_srt(
        cues, [result["translations"].get(s.index, s.content) for s in cues],
    )
    out.write_text(final, encoding="utf-8")
    n_err = result.get("n_failed", 0)
    print(f"  {tag}: {result['elapsed_s']:.0f}s · hits RAG inyectados={RAG_HITS['n']} "
          f"· cues [ERROR]={n_err} · → {out.name}")
    translation_service.build_user_prompt = _orig_build
    return out


# ─────────────────────────────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("[1/5] Cargando corpus desde SQLite…")
    corpus = load_corpus()
    print(f"  {len(corpus)} cues de corpus")

    print("[2/5] Generando descripciones de escena (V2, gpt-4o-mini)…")
    descs = await describe_chunks(corpus)
    ejemplo = next((d for d in descs.values() if d), "")
    print(f"  ejemplo: {ejemplo!r}")

    print("[3/5] Indexando colecciones…")
    await build_collection("exp_rag_v1", corpus, None)
    await build_collection("exp_rag_v2", corpus, descs)

    print("[4/5] Traduciendo CANDY con cada variante (secuencial, Tier 1)…")
    await run_variant("v1_vecinas", "exp_rag_v1", _prompt_v1_contado)
    print(f"  … sleep {SLEEP_BETWEEN_RUNS_S}s (TPM)")
    time.sleep(SLEEP_BETWEEN_RUNS_S)
    await run_variant("v2_descripciones", "exp_rag_v2", _prompt_v2)

    print("\n[5/5] Hecho. Evalúa con:")
    print("  uv run python eval/eval_against_human.py eval/_exp_candy_v1_vecinas.srt "
          '--en "<CANDY EN>" --hum "<CANDY humano>"')
    print("  (ídem v2_descripciones)")


if __name__ == "__main__":
    asyncio.run(main())
