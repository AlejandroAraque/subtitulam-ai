"""chrF (character n-gram F-score) — sensible a morfología, bueno para ES.

Complementa a BLEU en dos sentidos:
  - Tolera mejor variaciones morfológicas (corro/corres/corre).
  - Funciona en segmentos cortos donde BLEU colapsa por falta de n-gramas.
"""
import sacrebleu


def _normalize(text: str) -> str:
    """Misma normalización que bleu.py: aplanar cues multi-línea."""
    return text.replace("\n", " ").strip()


def compute(
    predictions: list[str],
    references: list[str],
    **kwargs,
) -> dict:
    """
    chrF corpus-level. Mide overlap de n-gramas de caracteres.

    Args:
        predictions: lista de cues traducidos por el sistema.
        references: lista de cues de referencia humana, alineada 1:1.

    Returns:
        {
            "chrf":      float,   # F-score 0..100 (típico 50-80 sistemas razonables)
            "chrf_n":    int,     # tamaño del n-grama de caracteres (default 6)
            "chrf_beta": int,     # peso recall vs precisión (default 2)
        }
    """
    if len(predictions) != len(references):
        raise ValueError(
            f"predictions ({len(predictions)}) y references ({len(references)}) "
            "deben tener la misma longitud."
        )

    preds = [_normalize(p) for p in predictions]
    refs  = [_normalize(r) for r in references]

    chrf = sacrebleu.corpus_chrf(preds, [refs])

    return {
        "chrf":      round(chrf.score, 2),
        "chrf_n":    chrf.char_order,
        "chrf_beta": chrf.beta,
    }
