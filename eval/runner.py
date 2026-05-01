"""
Orquestador de evaluación.

Recibe un RunConfig, carga el test-set, traduce cada par via
translation_service (llamada directa, sin HTTP), calcula todas las
métricas registradas y devuelve un RunResult serializable.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from app.core.config   import DATA_DIR
from app.core.database import SessionLocal
from app.services      import glossary_service, translation_service
from eval.config       import RunConfig, RunResult, now_iso
from eval.metrics      import bleu, chrf, cpl, glossary


# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_TESTSET_PATH = DATA_DIR / "testset" / "reference_en_es.jsonl"
DEFAULT_RUNS_DIR     = DATA_DIR / "eval_runs"


# ── Helpers internos ────────────────────────────────────────────────────────

def _load_testset(
    path: Path,
    filter_dataset: str | None = None,
) -> list[dict[str, Any]]:
    """Carga el JSONL del test-set, opcionalmente filtrando por source_dataset.

    Args:
        path: ruta al .jsonl.
        filter_dataset: si se pasa, solo devuelve pares cuyo campo
            source_dataset coincida (ej. 'wmt13', 'v1.1_bootstrap').
    """
    if not path.exists():
        raise FileNotFoundError(f"Test-set no encontrado: {path}")
    pairs: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    if filter_dataset:
        pairs = [p for p in pairs if p.get("source_dataset") == filter_dataset]
        if not pairs:
            raise ValueError(
                f"Filtro source_dataset='{filter_dataset}' no produjo ningún par. "
                f"Revisa los valores en {path.name}."
            )
    return pairs


def _get_git_commit() -> str:
    """Hash corto del commit actual. 'unknown' si no estamos en un repo."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _load_glossary_from_db() -> list[dict[str, Any]]:
    """Lee el glosario actual de SQLite. [] si no hay nada."""
    db = SessionLocal()
    try:
        return [t.to_dict() for t in glossary_service.list_terms(db)]
    finally:
        db.close()


# ── API pública ─────────────────────────────────────────────────────────────

def run(
    config: RunConfig,
    testset_path: Path = DEFAULT_TESTSET_PATH,
    filter_dataset: str | None = None,
) -> RunResult:
    """Ejecuta una evaluación completa con la configuración dada.

    Pasos:
      1. Carga el test-set (opcionalmente filtrado por source_dataset).
      2. Llama a translation_service.translate_texts (asíncrono).
      3. Lee el glosario actual de SQLite.
      4. Ejecuta las 4 métricas registradas.
      5. Devuelve un RunResult serializable.

    Args:
        filter_dataset: si se pasa, solo evalúa pares cuyo source_dataset
            coincida (ej. 'wmt13' para excluir bootstrap contaminado).

    No persiste a disco — usa save() después si quieres guardar el JSON.
    """
    pairs      = _load_testset(testset_path, filter_dataset=filter_dataset)
    sources    = [p["source"] for p in pairs]
    references = [p["target"] for p in pairs]

    # translate_texts espera un dict {idx: texto} y devuelve un dict
    # {translations, tokens_prompt, tokens_completion, elapsed_s}.
    # job_id=None y sliding_window=0: no contamina ChromaDB y los cues
    # del test-set son samples sueltos (no narrativa secuencial).
    texts_dict = {i + 1: src for i, src in enumerate(sources)}
    trans_out  = asyncio.run(translation_service.translate_texts(
        texts_dict,
        target_lang=config.target_lang,
        context=config.context,
        cpl_limit=config.cpl_limit,
        chunk_size=config.chunk_size,
        job_id=None,
        use_rag=config.use_rag,
        sliding_window_size=0,
    ))

    # Reconstruir predictions en el mismo orden que sources
    predictions = [trans_out["translations"].get(i + 1, "") for i in range(len(sources))]

    # Glosario actual (puede estar vacío en v1.5 — la métrica devolverá None)
    db_glossary = _load_glossary_from_db()

    # Aplicar las 4 métricas. Cada una añade sus claves al dict acumulado.
    metrics: dict[str, Any] = {}
    metrics.update(bleu.compute    (predictions, references))
    metrics.update(chrf.compute    (predictions, references))
    metrics.update(cpl.compute     (predictions, cpl_limit=config.cpl_limit))
    metrics.update(glossary.compute(predictions, sources=sources, glossary=db_glossary))

    return RunResult(
        config            = config,
        timestamp         = now_iso(),
        git_commit        = _get_git_commit(),
        n_pairs           = len(pairs),
        elapsed_s         = round(trans_out["elapsed_s"], 3),
        tokens_prompt     = trans_out["tokens_prompt"],
        tokens_completion = trans_out["tokens_completion"],
        metrics           = metrics,
        predictions       = predictions,
    )


def run_from_predictions(
    predictions: list[str],
    config: RunConfig,
    *,
    testset_path: Path = DEFAULT_TESTSET_PATH,
    filter_dataset: str | None = None,
    elapsed_s: float = 0.0,
    tokens_prompt: int = 0,
    tokens_completion: int = 0,
) -> RunResult:
    """Re-evalúa predicciones existentes sin llamar a OpenAI.

    Útil para:
      - Añadir métricas nuevas (BERTScore en fase 7) a un run histórico.
      - Recomputar CPL con un límite distinto sin re-traducir.
      - Evaluar predicciones generadas por otro sistema externo.

    Args:
        predictions: traducciones ya generadas, alineadas 1:1 con el test-set.
        config: configuración a registrar en el RunResult (típicamente la
            del run original con name + "_reeval").
        filter_dataset: filtrar el test-set por source_dataset (debe coincidir
            con el filtro usado en el run original para que las predicciones
            sigan alineadas 1:1).
        elapsed_s, tokens_*: se propagan tal cual del run original para no
            perder la trazabilidad de coste.
    """
    pairs      = _load_testset(testset_path, filter_dataset=filter_dataset)
    sources    = [p["source"] for p in pairs]
    references = [p["target"] for p in pairs]

    if len(predictions) != len(pairs):
        raise ValueError(
            f"predictions ({len(predictions)}) y test-set ({len(pairs)}) "
            "no coinciden en número de pares."
        )

    db_glossary = _load_glossary_from_db()

    metrics: dict[str, Any] = {}
    metrics.update(bleu.compute    (predictions, references))
    metrics.update(chrf.compute    (predictions, references))
    metrics.update(cpl.compute     (predictions, cpl_limit=config.cpl_limit))
    metrics.update(glossary.compute(predictions, sources=sources, glossary=db_glossary))

    return RunResult(
        config            = config,
        timestamp         = now_iso(),
        git_commit        = _get_git_commit(),
        n_pairs           = len(pairs),
        elapsed_s         = elapsed_s,
        tokens_prompt     = tokens_prompt,
        tokens_completion = tokens_completion,
        metrics           = metrics,
        predictions       = predictions,
    )


def load_run(path: Path) -> dict[str, Any]:
    """Carga el JSON de un RunResult guardado por save()."""
    if not path.exists():
        raise FileNotFoundError(f"Run no encontrado: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save(result: RunResult, runs_dir: Path = DEFAULT_RUNS_DIR) -> Path:
    """Persiste el RunResult como JSON en runs_dir, devuelve la ruta usada.

    Nombre: {YYYY-MM-DD_HHMMSS}_{config.name}.json
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = result.config.name.replace("+", "plus").replace(" ", "_")
    ts        = result.timestamp.replace(":", "").replace("-", "")[:15]
    fname     = f"{ts}_{safe_name}.json"
    path      = runs_dir / fname
    path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
