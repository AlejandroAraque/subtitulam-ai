"""
CLI: argparse + invocación del runner.

Uso:
    python -m eval --config baseline
    python -m eval --config baseline --no-save
    python -m eval --config "context_test" --context "Breaking Bad: serie dramatica"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eval.config import RunConfig, RunResult
from eval.runner import run, run_from_predictions, load_run, save, DEFAULT_TESTSET_PATH

# Forzar UTF-8 en stdout/stderr — necesario en PowerShell (cp1252 por
# defecto) para imprimir los caracteres Unicode del marco (─ │ ╭ ╰).
# Inocuo en Linux/Mac.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass


# ── Formateo de la salida ────────────────────────────────────────────────────

def _fmt_metric(value, suffix: str = "") -> str:
    """Formatea None como 'N/A', float como 2 decimales, int sin decimales."""
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _print_header(args: argparse.Namespace, n_pairs: int, git_commit: str) -> None:
    print("─" * 60)
    print("  Subtitulam · Evaluación")
    print("─" * 60)
    print(f"  config:       {args.config}")
    print(f"  target_lang:  {args.target_lang}")
    print(f"  cpl_limit:    {args.cpl}")
    ctx = args.context if args.context else "(vacío)"
    if len(ctx) > 50:
        ctx = ctx[:50] + "…"
    print(f"  context:      {ctx}")
    print(f"  testset:      {args.testset} ({n_pairs} pares)")
    print(f"  git commit:   {git_commit}")
    print()


def _print_metrics(result: RunResult) -> None:
    m = result.metrics
    print()
    print("  ╭──────────────────────────────────────────╮")
    print("  │  Métricas                                  │")
    print("  ├──────────────────────────────────────────┤")
    print(f"  │  BLEU                          {_fmt_metric(m.get('bleu')):>10}│")
    print(f"  │  chrF                          {_fmt_metric(m.get('chrf')):>10}│")
    print(f"  │  CPL compliance               {_fmt_metric(m.get('cpl_compliance'), '%'):>11}│")
    adh = m.get('glossary_adherence')
    n_terms = m.get('n_terms_in_glossary', 0)
    n_opps  = m.get('n_opportunities', 0)
    adh_str = _fmt_metric(adh, '%') if adh is not None else f"N/A ({n_terms} térm.)"
    print(f"  │  Glossary adherence           {adh_str:>11}│")
    if adh is not None:
        applied = m.get('n_applied', 0)
        print(f"  │     ({applied}/{n_opps} oportunidades aplicadas)         │")
    print("  ├──────────────────────────────────────────┤")
    tokens_total = result.tokens_prompt + result.tokens_completion
    print(f"  │  Tokens: {result.tokens_prompt} + {result.tokens_completion} = {tokens_total:<19}│")
    print(f"  │  Latencia: {result.elapsed_s:.2f}s {' '*23}│")
    print("  ╰──────────────────────────────────────────╯")


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m eval",
        description="Ejecuta una evaluación del sistema Subtitulam.",
    )
    parser.add_argument(
        "--config", default="baseline",
        help="Nombre lógico del run (se incluye en el filename JSON). Default: baseline",
    )
    parser.add_argument(
        "--target-lang", default="es",
        help="Idioma destino. Default: es",
    )
    parser.add_argument(
        "--cpl", type=int, default=42,
        help="Límite CPL. Default: 42",
    )
    parser.add_argument(
        "--context", default="",
        help="Contexto global a inyectar en el prompt. Default: (vacío)",
    )
    parser.add_argument(
        "--testset", default=str(DEFAULT_TESTSET_PATH),
        help=f"Ruta al test-set JSONL. Default: {DEFAULT_TESTSET_PATH}",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="No persistir el JSON del resultado, solo imprimir métricas.",
    )
    parser.add_argument(
        "--from-run", default=None,
        help="Re-evaluar las predicciones de un JSON guardado (sin llamar a OpenAI). "
             "Solo --cpl puede sobreescribirse; el resto del config viene del JSON.",
    )
    parser.add_argument(
        "--filter-dataset", default=None,
        help="Evaluar solo los pares cuyo campo source_dataset coincida "
             "(ej. 'wmt13' para excluir bootstrap contaminado, "
             "'v1.1_bootstrap' para evaluar solo el subset original).",
    )

    args = parser.parse_args()

    testset_path = Path(args.testset)
    filter_ds    = args.filter_dataset
    from eval.runner import _load_testset, _get_git_commit

    # ── MODO RE-EVAL ────────────────────────────────────────────────────
    if args.from_run:
        try:
            run_data = load_run(Path(args.from_run))
        except Exception as e:
            print(f"ERROR cargando --from-run: {e}", file=sys.stderr)
            sys.exit(1)

        # Reconstruir config a partir del JSON, permitiendo override de --cpl
        original_cfg = run_data["config"]
        config = RunConfig(
            name        = original_cfg["name"] + "_reeval",
            target_lang = original_cfg["target_lang"],
            cpl_limit   = args.cpl,                           # ← único override permitido
            context     = original_cfg.get("context", ""),
            chunk_size  = original_cfg.get("chunk_size", 5),
        )

        # Sobrescribir args.* para que el header refleje lo que se va a evaluar
        args.config      = config.name
        args.target_lang = config.target_lang
        args.context     = config.context
        n_pairs          = run_data["n_pairs"]

        _print_header(args, n_pairs, _get_git_commit())
        print(f"  Modo --from-run: usando predictions de '{Path(args.from_run).name}'")
        print(f"  (no se llama a OpenAI · CPL override = {args.cpl})\n")

        try:
            result = run_from_predictions(
                predictions       = run_data["predictions"],
                config            = config,
                testset_path      = testset_path,
                filter_dataset    = filter_ds,
                elapsed_s         = run_data.get("elapsed_s", 0.0),
                tokens_prompt     = run_data.get("tokens_prompt", 0),
                tokens_completion = run_data.get("tokens_completion", 0),
            )
        except Exception as e:
            print(f"\n  ERROR re-evaluando: {e}", file=sys.stderr)
            sys.exit(2)

    # ── MODO NORMAL: traducir + evaluar ──────────────────────────────────
    else:
        config = RunConfig(
            name        = args.config,
            target_lang = args.target_lang,
            cpl_limit   = args.cpl,
            context     = args.context,
        )

        try:
            n_pairs = len(_load_testset(testset_path, filter_dataset=filter_ds))
        except Exception as e:
            print(f"ERROR cargando test-set: {e}", file=sys.stderr)
            sys.exit(1)

        _print_header(args, n_pairs, _get_git_commit())
        if filter_ds:
            print(f"  Filtro: source_dataset='{filter_ds}'")
        print("  Traduciendo... (esto puede tardar ~10-20s)\n")

        try:
            result = run(config, testset_path=testset_path, filter_dataset=filter_ds)
        except Exception as e:
            print(f"\n  ERROR durante la ejecución: {e}", file=sys.stderr)
            sys.exit(2)

    _print_metrics(result)

    # Persistencia
    if not args.no_save:
        out_path = save(result)
        try:
            rel = out_path.relative_to(Path.cwd())
        except ValueError:
            rel = out_path
        print(f"\n  Guardado en: {rel}\n")
    else:
        print("\n  (--no-save activo, no se guardó el JSON)\n")
