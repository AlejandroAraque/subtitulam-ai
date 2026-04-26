"""BLEU corpus-level usando sacrebleu (versión estándar de WMT)."""
import sacrebleu


def _normalize(text: str) -> str:
    """Aplana cues multi-línea para que la unidad evaluada sea el cue
    completo, no cada línea por separado. BLEU es sensible a la unidad
    de segmentación; comparar cues completos contra cues completos
    elimina ruido por diferencias en cómo se hicieron los cortes."""
    return text.replace("\n", " ").strip()


def compute(
    predictions: list[str],
    references: list[str],
    **kwargs,
) -> dict:
    """
    BLEU corpus-level. Compara predicciones contra una sola referencia
    por par (caso típico de MT supervisada).

    Args:
        predictions: lista de cues traducidos por el sistema.
        references: lista de cues de referencia humana, alineada 1:1.

    Returns:
        {
            "bleu":              float,        # score 0..100
            "bleu_brevity_penalty": float,     # 0..1, penaliza outputs cortos
            "bleu_sys_len":      int,          # longitud total del sistema
            "bleu_ref_len":      int,          # longitud total de la referencia
            "bleu_precisions":   list[float],  # precisión 1-, 2-, 3-, 4-gramas
        }
    """
    if len(predictions) != len(references):
        raise ValueError(
            f"predictions ({len(predictions)}) y references ({len(references)}) "
            "deben tener la misma longitud."
        )

    preds = [_normalize(p) for p in predictions]
    refs  = [_normalize(r) for r in references]

    # sacrebleu espera: corpus_bleu(hyps, [refs_set_1, refs_set_2, ...])
    # En nuestro caso solo hay una referencia por par → un solo conjunto.
    bleu = sacrebleu.corpus_bleu(preds, [refs])

    return {
        "bleu":                 round(bleu.score, 2),
        "bleu_brevity_penalty": round(bleu.bp, 4),
        "bleu_sys_len":         bleu.sys_len,
        "bleu_ref_len":         bleu.ref_len,
        "bleu_precisions":      [round(p, 2) for p in bleu.precisions],
    }
