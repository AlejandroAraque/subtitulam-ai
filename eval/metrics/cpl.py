"""Cumplimiento CPL — % de líneas de subtítulo bajo el límite definido."""
from typing import Iterable


def _split_into_lines(texts: Iterable[str]) -> list[str]:
    """Aplana una lista de cues (cada uno con posibles \\n internos) en
    líneas individuales no vacías. Las líneas vacías se descartan porque
    no se ven en pantalla."""
    lines: list[str] = []
    for txt in texts:
        for ln in txt.split("\n"):
            ln = ln.strip()
            if ln:
                lines.append(ln)
    return lines


def compute(
    predictions: list[str],
    references: list[str] | None = None,
    cpl_limit: int = 42,
) -> dict:
    """
    Calcula el % de líneas en `predictions` que cumplen el límite CPL.

    Args:
        predictions: lista de cues traducidos (cada uno puede contener
            saltos de línea internos para subtítulos multi-línea).
        references: aceptado por uniformidad de interfaz pero no se usa
            (CPL es una métrica intrínseca, no compara con referencia).
        cpl_limit: máximo de caracteres por línea visible. Default 42
            (estándar Netflix para subtítulos profesionales).

    Returns:
        {
            "cpl_compliance": float,   # porcentaje 0..100
            "n_lines_total":  int,
            "n_lines_over":   int,
            "max_line_len":   int,     # la línea más larga encontrada
            "cpl_limit":      int,     # eco del límite usado
        }
    """
    lines = _split_into_lines(predictions)
    n_total = len(lines)

    if n_total == 0:
        return {
            "cpl_compliance": 0.0,
            "n_lines_total":  0,
            "n_lines_over":   0,
            "max_line_len":   0,
            "cpl_limit":      cpl_limit,
        }

    n_over = sum(1 for ln in lines if len(ln) > cpl_limit)
    compliance = round((n_total - n_over) / n_total * 100, 2)
    max_len = max(len(ln) for ln in lines)

    return {
        "cpl_compliance": compliance,
        "n_lines_total":  n_total,
        "n_lines_over":   n_over,
        "max_line_len":   max_len,
        "cpl_limit":      cpl_limit,
    }
