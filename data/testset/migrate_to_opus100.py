"""
Script reproducible que migra el test-set v2.1.5 → v2.1.6:

  ANTES:  20 bootstrap + 30 WMT13 (noticias)         = 50 mezclados
  DESPUÉS: 20 bootstrap + 30 OPUS-100 (subtítulos)   = 50 todos subtitle-domain

Acciones:
  1. Elimina los entries con source_dataset='wmt13' del JSONL.
  2. Stream del split 'test' de OPUS-100 EN-ES (Helsinki-NLP/opus-100).
  3. Filtra por heurística "subtitle-likeness" (longitud + sin marcas
     burocráticas + carácter conversacional).
  4. Sample 30 con seed=42.
  5. Re-numera IDs 21-50 para los nuevos.
  6. Guarda el JSONL final con source_dataset='opus_100'.

Idempotente: si ya hay pares de OPUS-100 en el JSONL, sale sin tocar.

Uso:
    uv run python data/testset/migrate_to_opus100.py
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

# ── Configuración ────────────────────────────────────────────────────────────
HERE             = Path(__file__).parent
JSONL_PATH       = HERE / "reference_en_es.jsonl"
SEED             = 42
N_SAMPLE         = 30
MIN_WORDS_EN     = 3
MAX_WORDS_EN     = 20
DATASET_TAG_NEW  = "opus_100"
DATASET_TAG_OUT  = "wmt13"        # los entries que se van a eliminar
DATASET_TAG_KEEP = "v1.1_bootstrap"  # los que se conservan

# Patrones que delatan texto burocrático/legal (descartar)
BUREAUCRATIC_PATTERNS = [
    r'\([a-z]\)',                # (a), (b), (i)
    r'^\s*\d+\.\s',              # listas numeradas "1. ..."
    r'\barticle\b',              # "Article 5"
    r'\bparagraph\b',
    r'\bsubparagraph\b',
    r'\bclause\b',
    r'\bshall\s+be\s',           # legal "shall be"
    r'\bhereby\b',
    r'\bwhereas\b',
    r'\bpursuant\s+to\b',
]
_BUREAUCRATIC_RE = re.compile('|'.join(BUREAUCRATIC_PATTERNS), re.IGNORECASE)


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


def _is_subtitle_like(en: str, es: str) -> bool:
    """Heurística: aceptar solo pares con sabor a diálogo subtítulo."""
    n = len(en.split())
    if not (MIN_WORDS_EN <= n <= MAX_WORDS_EN):
        return False
    # Descartar si EN parece burocracia/legal
    if _BUREAUCRATIC_RE.search(en):
        return False
    # Descartar si ES parece burocracia/legal (a veces solo el ES lo trae)
    if _BUREAUCRATIC_RE.search(es):
        return False
    # Descartar pares con muchos números o paréntesis (no subtítulo típico)
    if sum(c.isdigit() for c in en) > 6:
        return False
    # Pares vacíos o solo puntuación
    if not en.strip() or not es.strip():
        return False
    # Strings idénticos suelen ser ruido
    if en.strip() == es.strip():
        return False
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    existing = _load_jsonl(JSONL_PATH)
    print(f"[1/6] Cargados {len(existing)} pares existentes")

    # Idempotencia
    if any(p.get("source_dataset") == DATASET_TAG_NEW for p in existing):
        n = sum(1 for p in existing if p.get("source_dataset") == DATASET_TAG_NEW)
        print(f"      Ya hay {n} pares de OPUS-100 — script idempotente, saliendo")
        return 0

    # ── Eliminar WMT13 ──────────────────────────────────────────────────────
    n_before = len(existing)
    kept = [p for p in existing if p.get("source_dataset") != DATASET_TAG_OUT]
    n_dropped = n_before - len(kept)
    print(f"[2/6] Eliminados {n_dropped} pares con source_dataset='{DATASET_TAG_OUT}'")
    print(f"      Quedan {len(kept)} pares (esperado: 20 de '{DATASET_TAG_KEEP}')")

    # ── Cargar OPUS-100 en streaming y filtrar ──────────────────────────────
    print(f"[3/6] Streameando OPUS-100 EN-ES (split 'test', 2000 pares disponibles)…")
    try:
        from datasets import load_dataset
        ds = load_dataset("Helsinki-NLP/opus-100", "en-es", split="test", streaming=True)
    except Exception as e:
        print(f"      ERROR cargando OPUS-100: {e}", file=sys.stderr)
        return 1

    candidates: list[tuple[str, str]] = []
    n_seen = 0
    for item in ds:
        n_seen += 1
        en = item["translation"]["en"]
        es = item["translation"]["es"]
        if _is_subtitle_like(en, es):
            candidates.append((en, es))

    print(f"[4/6] Filtrados {len(candidates)}/{n_seen} candidatos subtitle-like")
    if len(candidates) < N_SAMPLE:
        print(f"      ERROR: insuficientes ({len(candidates)} < {N_SAMPLE})", file=sys.stderr)
        return 1

    # ── Sample reproducible ─────────────────────────────────────────────────
    random.seed(SEED)
    sample = random.sample(candidates, N_SAMPLE)
    print(f"[5/6] Sampleados {N_SAMPLE} pares con seed={SEED}")

    # ── Construir entries nuevos con IDs 21-50 ──────────────────────────────
    next_id = max((p["id"] for p in kept), default=0) + 1
    new_entries = [
        {
            "id":             next_id + i,
            "source":         en,
            "target":         es,
            "notes":          "OPUS-100 EN-ES test split, filtered subtitle-like (Helsinki-NLP/opus-100)",
            "source_dataset": DATASET_TAG_NEW,
        }
        for i, (en, es) in enumerate(sample)
    ]

    final = kept + new_entries
    _save_jsonl(JSONL_PATH, final)
    print(f"[6/6] Guardados {len(final)} pares en {JSONL_PATH.name} "
          f"({len(kept)} bootstrap + {len(new_entries)} opus_100)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
