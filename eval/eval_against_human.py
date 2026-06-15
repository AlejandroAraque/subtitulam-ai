"""Evaluación cuantitativa+cualitativa: IA Subtitulam vs traductor humano vs EN.

Compara 3 SRTs del mismo material:
  - EN:    original en inglés       (source)
  - HUM:   traducción humana        (ground truth profesional)
  - IA:    salida de Subtitulam     (hypothesis a evaluar)

Las traducciones humana e IA NO comparten timecodes exactos (el profesional
reajusta start/end por reading speed), así que la alineación se hace por
solapamiento de intervalos temporales [start, end] con tolerancia, no por
índice de cue.

Uso:
  python eval/eval_against_human.py <ia.srt>
  python eval/eval_against_human.py <ia.srt> --en <en.srt> --hum <hum.srt>

  Defaults (resolviendo en este orden):
    1) --en / --hum si vienen por CLI
    2) variables de entorno EVAL_EN_SRT / EVAL_HUM_SRT
    3) <ia_dir>/EN.srt y <ia_dir>/HUM.srt junto al archivo IA
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import sacrebleu

# ──────────────────────────────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Cue:
    idx:     int
    start_s: float     # segundos desde el inicio
    end_s:   float
    text:    str       # texto plano (sin tags HTML del SRT)

    @property
    def mid_s(self) -> float:
        return (self.start_s + self.end_s) / 2

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def char_count(self) -> int:
        return len(self.text.replace("\n", " "))


_TS_RE = re.compile(r"^(\d+):(\d+):(\d+)[,.](\d+)$")


def _ts_to_seconds(ts: str) -> float:
    m = _TS_RE.match(ts.strip())
    if not m:
        raise ValueError(f"bad timestamp: {ts!r}")
    h, mn, s, ms = m.groups()
    return int(h) * 3600 + int(mn) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: Path) -> list[Cue]:
    """Parser SRT minimal: tolera CRLF, BOM, líneas en blanco extra."""
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\r?\n", text.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = [ln.rstrip("\r") for ln in block.split("\n") if ln.strip()]
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0])
        except ValueError:
            continue
        tm = re.match(r"(\S+)\s*-->\s*(\S+)", lines[1])
        if not tm:
            continue
        start = _ts_to_seconds(tm.group(1))
        end   = _ts_to_seconds(tm.group(2))
        body  = "\n".join(lines[2:])
        cues.append(Cue(idx=idx, start_s=start, end_s=end, text=body))
    return cues


# ──────────────────────────────────────────────────────────────────────
# Alineación
# ──────────────────────────────────────────────────────────────────────

@dataclass
class AlignedRow:
    """Fila alineada: un cue EN puede mapear a 0 o 1 cue de cada lado.

    El humano puede omitir -> hum_text vacío. La IA siempre cubre (verificado).
    """
    en:       Cue
    hum:      Cue | None = None
    ia:       Cue | None = None

    def overlap_score(self, other: Cue) -> float:
        """Solapamiento como fracción del intervalo más corto (Jaccard-ish).
        Devuelve 0..1, donde 1 = solapamiento total."""
        a0, a1 = self.en.start_s, self.en.end_s
        b0, b1 = other.start_s, other.end_s
        inter = max(0.0, min(a1, b1) - max(a0, b0))
        if inter == 0:
            return 0.0
        shorter = min(a1 - a0, b1 - b0)
        if shorter <= 0:
            return 0.0
        return inter / shorter


def align_by_overlap(en_cues: list[Cue], es_cues: list[Cue],
                     min_overlap: float = 0.30,
                     max_mid_dist_s: float = 1.0) -> dict[int, Cue]:
    """Asigna cada cue inglés a su mejor match en español por solapamiento.

    Estrategia:
      1. Buscamos solapamiento de intervalos [start, end].
      2. Si ningún cue español solapa significativamente, buscamos el más
         cercano por centro temporal, dentro de una ventana de 1s.
      3. Si ni así, lo damos por omitido.

    Permite que varios EN apunten al mismo ES (caso "humano fusiona 2 cues").
    """
    mapping: dict[int, Cue] = {}
    for en in en_cues:
        best: tuple[float, Cue] | None = None
        for es in es_cues:
            inter = max(0.0, min(en.end_s, es.end_s) - max(en.start_s, es.start_s))
            shorter = min(en.duration_s, es.duration_s)
            if shorter <= 0:
                continue
            ov = inter / shorter
            if ov >= min_overlap:
                if best is None or ov > best[0]:
                    best = (ov, es)
        if best is not None:
            mapping[en.idx] = best[1]
            continue
        # Fallback: el más cercano por centro
        nearest: tuple[float, Cue] | None = None
        for es in es_cues:
            d = abs(en.mid_s - es.mid_s)
            if d <= max_mid_dist_s:
                if nearest is None or d < nearest[0]:
                    nearest = (d, es)
        if nearest is not None:
            mapping[en.idx] = nearest[1]
    return mapping


# ──────────────────────────────────────────────────────────────────────
# Métricas
# ──────────────────────────────────────────────────────────────────────

@dataclass
class CoverageStats:
    n_en:                int
    n_hum:               int
    n_ia:                int
    hum_aligned_to_en:   int     # cues EN que tienen cue humano alineado
    ia_aligned_to_en:    int
    hum_omissions:       list[int]  # idx EN sin match humano
    ia_omissions:        list[int]


def coverage(en: list[Cue], hum: list[Cue], ia: list[Cue],
             hum_map: dict[int, Cue], ia_map: dict[int, Cue]) -> CoverageStats:
    hum_aligned = sum(1 for c in en if c.idx in hum_map)
    ia_aligned  = sum(1 for c in en if c.idx in ia_map)
    return CoverageStats(
        n_en=len(en), n_hum=len(hum), n_ia=len(ia),
        hum_aligned_to_en=hum_aligned, ia_aligned_to_en=ia_aligned,
        hum_omissions=[c.idx for c in en if c.idx not in hum_map],
        ia_omissions=[c.idx for c in en if c.idx not in ia_map],
    )


@dataclass
class LengthStats:
    avg_chars_hum:  float
    avg_chars_ia:   float
    avg_chars_en:   float
    cpl_max_hum:    int
    cpl_max_ia:     int
    cpl_compliance_hum_38: float  # % de líneas <= 38 chars
    cpl_compliance_ia_38:  float


def length_stats(en: list[Cue], hum: list[Cue], ia: list[Cue],
                 cpl_limit: int = 38) -> LengthStats:
    def avg_chars(cs: Iterable[Cue]) -> float:
        cs_list = list(cs)
        if not cs_list:
            return 0.0
        return sum(c.char_count for c in cs_list) / len(cs_list)

    def max_line(cs: Iterable[Cue]) -> int:
        m = 0
        for c in cs:
            for ln in c.text.split("\n"):
                if len(ln) > m:
                    m = len(ln)
        return m

    def cpl_pct(cs: Iterable[Cue], limit: int) -> float:
        total = 0
        good = 0
        for c in cs:
            for ln in c.text.split("\n"):
                if not ln.strip():
                    continue
                total += 1
                if len(ln) <= limit:
                    good += 1
        return 100.0 * good / max(1, total)

    return LengthStats(
        avg_chars_hum=avg_chars(hum),
        avg_chars_ia=avg_chars(ia),
        avg_chars_en=avg_chars(en),
        cpl_max_hum=max_line(hum),
        cpl_max_ia=max_line(ia),
        cpl_compliance_hum_38=cpl_pct(hum, cpl_limit),
        cpl_compliance_ia_38=cpl_pct(ia, cpl_limit),
    )


@dataclass
class TranslationQualityStats:
    """BLEU y chrF de la IA usando el humano como referencia.

    Solo se incluyen pares donde AMBOS lados tienen cue (intersección de
    alineaciones). Las omisiones humanas no penalizan a la IA aquí porque
    no hay nada con qué comparar.
    """
    n_pairs:     int
    bleu:        float
    chrf:        float


def translation_quality(en: list[Cue],
                        hum_map: dict[int, Cue],
                        ia_map: dict[int, Cue]) -> TranslationQualityStats:
    hyps: list[str] = []
    refs: list[str] = []
    for c in en:
        if c.idx in hum_map and c.idx in ia_map:
            hyps.append(ia_map[c.idx].text.replace("\n", " "))
            refs.append(hum_map[c.idx].text.replace("\n", " "))
    if not hyps:
        return TranslationQualityStats(0, 0.0, 0.0)
    bleu = sacrebleu.corpus_bleu(hyps, [refs]).score
    chrf = sacrebleu.corpus_chrf(hyps, [refs]).score
    return TranslationQualityStats(n_pairs=len(hyps), bleu=bleu, chrf=chrf)


# ──────────────────────────────────────────────────────────────────────
# Análisis cualitativo
# ──────────────────────────────────────────────────────────────────────

def comparison_table(en: list[Cue],
                     hum_map: dict[int, Cue],
                     ia_map: dict[int, Cue]) -> list[dict]:
    """Devuelve filas con (idx, ts, en, hum, ia) para cada cue EN.
    El caller selecciona ejemplos relevantes para la memoria."""
    rows = []
    for c in en:
        hum = hum_map.get(c.idx)
        ia  = ia_map.get(c.idx)
        rows.append({
            "idx":   c.idx,
            "start": c.start_s,
            "en":    c.text.replace("\n", " "),
            "hum":   hum.text.replace("\n", " ") if hum else "",
            "ia":    ia.text.replace("\n", " ") if ia else "",
            "hum_omitted": hum is None,
        })
    return rows


def normalized_diff(a: str, b: str) -> float:
    """Edit-distance ratio simple (Levenshtein normalizado, 0..1)."""
    if not a and not b:
        return 0.0
    # algoritmo iterativo, espacio O(min(n, m))
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1] / max(len(a), len(b))


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def _resolve_paths() -> tuple[Path, Path, Path]:
    """Resuelve las 3 rutas a partir de CLI / env / heurística vecina al IA."""
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ia", type=Path, help="SRT de la IA a evaluar")
    parser.add_argument("--en",  type=Path, default=None, help="SRT original en inglés")
    parser.add_argument("--hum", type=Path, default=None, help="SRT traducción humana de referencia")
    args = parser.parse_args()

    ia_path = args.ia if args.ia.is_absolute() else (Path.cwd() / args.ia)
    if not ia_path.is_file():
        parser.error(f"IA SRT no encontrado: {ia_path}")

    def _resolve(arg_val: Path | None, env_var: str, neighbour_name: str) -> Path:
        if arg_val is not None:
            return arg_val if arg_val.is_absolute() else Path.cwd() / arg_val
        env_val = os.getenv(env_var, "")
        if env_val:
            return Path(env_val)
        return ia_path.parent / neighbour_name

    en_path  = _resolve(args.en,  "EVAL_EN_SRT",  "EN.srt")
    hum_path = _resolve(args.hum, "EVAL_HUM_SRT", "HUM.srt")
    for label, p in [("EN", en_path), ("HUM", hum_path)]:
        if not p.is_file():
            parser.error(f"{label} SRT no encontrado: {p}  "
                         f"(pasar --{label.lower()} o set EVAL_{label}_SRT)")
    return en_path, hum_path, ia_path


def main() -> None:
    en_path, hum_path, ia_path = _resolve_paths()
    print(f"[info] EN : {en_path}")
    print(f"[info] HUM: {hum_path}")
    print(f"[info] IA : {ia_path}")
    en  = parse_srt(en_path)
    hum = parse_srt(hum_path)
    ia  = parse_srt(ia_path)

    hum_map = align_by_overlap(en, hum)
    ia_map  = align_by_overlap(en, ia)

    cov   = coverage(en, hum, ia, hum_map, ia_map)
    lens  = length_stats(en, hum, ia)
    quality = translation_quality(en, hum_map, ia_map)
    rows  = comparison_table(en, hum_map, ia_map)

    print("\n=== COVERAGE ===")
    print(f"cues EN={cov.n_en} | HUM={cov.n_hum} | IA={cov.n_ia}")
    print(f"alineados HUM->EN: {cov.hum_aligned_to_en}/{cov.n_en} "
          f"({100*cov.hum_aligned_to_en/cov.n_en:.1f}%)")
    print(f"alineados IA->EN:  {cov.ia_aligned_to_en}/{cov.n_en} "
          f"({100*cov.ia_aligned_to_en/cov.n_en:.1f}%)")
    print(f"omisiones humano: {cov.hum_omissions}")

    print("\n=== LENGTHS ===")
    print(f"avg chars/cue | EN={lens.avg_chars_en:.1f} | "
          f"HUM={lens.avg_chars_hum:.1f} | IA={lens.avg_chars_ia:.1f}")
    print(f"línea más larga | HUM={lens.cpl_max_hum} | IA={lens.cpl_max_ia}")
    print(f"CPL<=38 compliance | HUM={lens.cpl_compliance_hum_38:.1f}% | "
          f"IA={lens.cpl_compliance_ia_38:.1f}%")

    print("\n=== TRANSLATION QUALITY (humano como referencia) ===")
    print(f"pares evaluados: {quality.n_pairs}")
    print(f"BLEU: {quality.bleu:.2f}")
    print(f"chrF: {quality.chrf:.2f}")

    print("\n=== TOP DIVERGENCIAS (ratio edit-distance HUM vs IA) ===")
    div_rows = [
        (r["idx"], r["start"], r["en"], r["hum"], r["ia"],
         normalized_diff(r["hum"], r["ia"]))
        for r in rows if r["hum"] and r["ia"]
    ]
    div_rows.sort(key=lambda x: x[5], reverse=True)
    for idx, ts, en_t, hum_t, ia_t, d in div_rows[:25]:
        print(f"\n[{idx:>3} @ {ts:6.2f}s | d={d:.2f}]")
        print(f"  EN : {en_t}")
        print(f"  HUM: {hum_t}")
        print(f"  IA : {ia_t}")

    print("\n=== OMISIONES HUMANAS (cues que el traductor decidió no traducir) ===")
    for idx in cov.hum_omissions:
        r = next(r for r in rows if r["idx"] == idx)
        print(f"  [{idx:>3} @ {r['start']:6.2f}s] EN: {r['en']!r}  IA: {r['ia']!r}")


if __name__ == "__main__":
    main()
