"""
Script reproducible para extender data/testset/reference_en_es.jsonl con
30 pares EN-ES del test-set oficial de WMT13.

Motivación: el test-set v1.0 (20 pares) se construyó bootstrapeando salidas
de v1.1 con correcciones manuales, lo que sesga BLEU/chrF al alza. Los
pares de WMT13 son traducciones humanas profesionales independientes,
imposibles de "filtrar" por construcción a través de nuestro sistema.

Comportamiento:
  - Idempotente: si los pares de WMT13 ya están añadidos, no hace nada.
  - Tagging: añade source_dataset='v1.1_bootstrap' a los entries antiguos
    si aún no lo tienen, para permitir filtrado posterior.
  - Reproducible: seed fija (42) y filtros explícitos.

Uso:
    uv run python data/testset/extend_with_wmt13.py
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

# ── Configuración ────────────────────────────────────────────────────────────
HERE             = Path(__file__).parent
JSONL_PATH       = HERE / "reference_en_es.jsonl"
SEED             = 42
N_SAMPLE         = 30
MIN_WORDS_EN     = 5     # descartar frases demasiado cortas
MAX_WORDS_EN     = 30    # descartar frases demasiado largas (típico WMT noticias)
WMT_TESTSET      = "wmt13"
WMT_LANGPAIR     = "en-es"
DATASET_TAG_NEW  = "wmt13"
DATASET_TAG_OLD  = "v1.1_bootstrap"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    pairs: list[dict] = []
    if not path.exists():
        return pairs
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    return pairs


def _save_jsonl(path: Path, pairs: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def _fetch_wmt(testset: str, langpair: str, side: str) -> list[str]:
    """Llama al CLI de sacrebleu para extraer src o ref del test-set."""
    cmd = ["uv", "run", "sacrebleu", "-t", testset, "-l", langpair, "--echo", side]
    out = subprocess.check_output(cmd, text=True, encoding="utf-8")
    return [ln for ln in out.splitlines() if ln.strip()]


def _filter_pair(en: str, es: str) -> bool:
    """Filtro de longitud: descarta frases muy cortas o muy largas."""
    n = len(en.split())
    return MIN_WORDS_EN <= n <= MAX_WORDS_EN


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    existing = _load_jsonl(JSONL_PATH)
    print(f"[1/5] Cargados {len(existing)} pares existentes desde {JSONL_PATH.name}")

    # ── Tagging idempotente de los pares existentes ─────────────────────────
    n_tagged = 0
    for p in existing:
        if "source_dataset" not in p:
            # Si ya hay pares de WMT13 (volveriamos a correr), no los re-tagueamos
            p["source_dataset"] = DATASET_TAG_OLD
            n_tagged += 1
    if n_tagged:
        print(f"      Tag '{DATASET_TAG_OLD}' añadido a {n_tagged} pares antiguos")
    else:
        print("      Todos los pares ya tienen source_dataset (ok)")

    # ── Comprobar idempotencia: si ya hay WMT13, salimos ────────────────────
    already_wmt = [p for p in existing if p.get("source_dataset") == DATASET_TAG_NEW]
    if already_wmt:
        print(f"[2/5] Ya hay {len(already_wmt)} pares de WMT13 — guardando tags y saliendo (idempotente)")
        _save_jsonl(JSONL_PATH, existing)
        return 0

    # ── Descargar WMT13 vía sacrebleu CLI ───────────────────────────────────
    print(f"[2/5] Descargando {WMT_TESTSET} {WMT_LANGPAIR} (puede tardar la 1ª vez)…")
    try:
        srcs = _fetch_wmt(WMT_TESTSET, WMT_LANGPAIR, "src")
        refs = _fetch_wmt(WMT_TESTSET, WMT_LANGPAIR, "ref")
    except subprocess.CalledProcessError as e:
        print(f"      ERROR al descargar: {e}", file=sys.stderr)
        return 1

    if len(srcs) != len(refs):
        print(f"      ERROR: src ({len(srcs)}) != ref ({len(refs)})", file=sys.stderr)
        return 1
    print(f"      Descargados {len(srcs)} pares brutos")

    # ── Filtrar por longitud razonable ──────────────────────────────────────
    candidates = [
        (en, es) for en, es in zip(srcs, refs) if _filter_pair(en, es)
    ]
    print(f"[3/5] Filtrados por longitud [{MIN_WORDS_EN}-{MAX_WORDS_EN}] palabras: "
          f"{len(candidates)}/{len(srcs)} candidatos válidos")

    if len(candidates) < N_SAMPLE:
        print(f"      ERROR: insuficientes candidatos ({len(candidates)} < {N_SAMPLE})", file=sys.stderr)
        return 1

    # ── Sample reproducible ─────────────────────────────────────────────────
    random.seed(SEED)
    sample = random.sample(candidates, N_SAMPLE)
    print(f"[4/5] Sampleados {N_SAMPLE} pares con seed={SEED}")

    # ── Construir las nuevas entradas y appendear ───────────────────────────
    next_id = max((p["id"] for p in existing), default=0) + 1
    new_entries = [
        {
            "id":             next_id + i,
            "source":         en,
            "target":         es,
            "notes":          "WMT13 EN-ES test set, traducción humana profesional independiente",
            "source_dataset": DATASET_TAG_NEW,
        }
        for i, (en, es) in enumerate(sample)
    ]

    extended = existing + new_entries
    _save_jsonl(JSONL_PATH, extended)
    print(f"[5/5] Guardados {len(extended)} pares en {JSONL_PATH.name} "
          f"({len(existing)} antiguos + {len(new_entries)} nuevos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
