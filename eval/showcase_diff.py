"""
Compara las traducciones del showcase entre las distintas configuraciones de
v2.5 (baseline / +RAG+SW / +Glosario / Full+AutoCtx) y produce:

  1. Una tabla markdown resumen con tokens, latencia, CPL compliance y
     número de cues que difieren respecto al baseline.
  2. Una lista de los cues con más variantes distintas entre configs,
     útiles para el diff cualitativo de la memoria del TFM.

Lee `data/showcase/runs/v2.5_*_stats.json` y los `.srt` asociados.
La primera config en orden alfabético se toma como baseline.

Uso:
    uv run python -m eval.showcase_diff
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "data" / "showcase" / "runs"

TS_RE = re.compile(
    r'\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}'
)


def parse_srt(path: Path) -> dict[int, str]:
    """{idx: texto del cue} preservando saltos de línea internos."""
    text = path.read_text(encoding="utf-8")
    cues: dict[int, str] = {}
    for blk in re.split(r'\n\s*\n', text):
        lines = blk.strip().splitlines()
        if (
            len(lines) >= 3
            and lines[0].strip().isdigit()
            and TS_RE.match(lines[1].strip())
        ):
            cues[int(lines[0])] = '\n'.join(lines[2:]).strip()
    return cues


def main() -> int:
    pattern = "v2.5_*_stats.json"
    stats_files = sorted(RUNS.glob(pattern))
    if not stats_files:
        print(f"ERROR: no hay {pattern} en {RUNS}", file=sys.stderr)
        return 1

    configs: list[dict] = []
    for sf in stats_files:
        stats = json.loads(sf.read_text(encoding="utf-8"))
        srt_path = sf.with_name(
            sf.name.replace("_stats.json", "_showcase_es.srt")
        )
        if not srt_path.exists():
            print(f"AVISO: falta {srt_path.name}, lo ignoro", file=sys.stderr)
            continue
        configs.append({
            "name":  stats["version"],
            "stats": stats,
            "cues":  parse_srt(srt_path),
        })

    if not configs:
        print("ERROR: no se cargó ninguna config", file=sys.stderr)
        return 2

    # Baseline = la primera (alfabéticamente, debería ser c1_baseline).
    baseline = configs[0]
    n_cues = len(baseline["cues"])

    # Cues distintos al baseline por config (igualdad estricta tras strip).
    for c in configs:
        if c is baseline:
            c["diff_count"] = 0
            continue
        c["diff_count"] = sum(
            1
            for idx, txt in c["cues"].items()
            if idx in baseline["cues"]
            and baseline["cues"][idx].strip() != txt.strip()
        )

    # ── Tabla markdown ────────────────────────────────────────────────
    print(f"## Ablación showcase v2.5 — {n_cues} cues sobre `{baseline['stats']['src']}`\n")
    print("| Config | RAG | SW | Glos | Tokens | Latencia (s) | CPL % | Cues ≠ baseline |")
    print("|---|:---:|:---:|:---:|---:|---:|---:|---:|")
    for c in configs:
        s = c["stats"]
        rag  = "✓" if s["use_rag"] else "—"
        sw   = str(s["sliding_window"])    if s["sliding_window"]    else "—"
        glos = str(s["n_glossary_terms"]) if s["n_glossary_terms"] else "—"
        diff = "—" if c is baseline else f'{c["diff_count"]} ({c["diff_count"]*100//n_cues}%)'
        name = c["name"].replace("v2.5_", "")
        print(
            f"| {name} | {rag} | {sw} | {glos} "
            f"| {s['tokens_total']:,} | {s['elapsed_s']} "
            f"| {s['cpl_compliance']} | {diff} |"
        )

    # ── Cues con más variantes distintas ───────────────────────────────
    cue_variance: list[tuple[int, int]] = []
    for idx in sorted(baseline["cues"]):
        variants = {c["cues"].get(idx, "").strip() for c in configs}
        if len(variants) > 1:
            cue_variance.append((idx, len(variants)))
    cue_variance.sort(key=lambda x: (-x[1], x[0]))

    print(
        f"\n## Cues con variantes distintas: "
        f"{len(cue_variance)} de {n_cues} ({len(cue_variance)*100//n_cues}%)\n"
    )

    # Mostrar los 8 cues con más variantes (top diff cualitativo).
    top = cue_variance[:8]
    if not top:
        print("_Todos los cues son idénticos entre configs — nada que comparar._")
        return 0

    for idx, nv in top:
        print(f"### Cue {idx}  · {nv} variantes")
        for c in configs:
            tag = c["name"].replace("v2.5_", "")
            txt = c["cues"].get(idx, "_[ausente]_").replace("\n", " ⏎ ")
            print(f"- **{tag}** — {txt}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
