"""Adherencia al glosario — % de términos aplicados cuando debían.

A diferencia del resto de métricas, ésta NO compara con la referencia
humana; verifica que el sistema aplicó las equivalencias obligatorias
del glosario en aquellos cues donde el término origen aparece.

Sirve como **termómetro de v2.4** (inyección del glosario en el prompt):
si la adherencia no sube respecto a v1.5, la inyección no aporta valor.
"""
from typing import Optional


def compute(
    predictions: list[str],
    references: list[str] | None = None,
    *,
    sources: Optional[list[str]] = None,
    glossary: Optional[list[dict]] = None,
    **kwargs,
) -> dict:
    """
    Calcula adherencia al glosario.

    Args:
        predictions: cues traducidos por el sistema.
        references: aceptado por uniformidad de interfaz, no se usa.
        sources: cues originales (en inglés). Necesarios para detectar
            qué términos del glosario se aplican en cada cue.
        glossary: lista de dicts {"source": str, "target": str, ...}.

    Returns:
        {
            "glossary_adherence":     float,  # % 0..100, o None si N/A
            "n_opportunities":        int,    # apariciones del source en cues
            "n_applied":              int,    # veces que el target apareció
            "n_terms_in_glossary":    int,
            "missed_terms":           list[dict],  # qué falló y dónde
        }
    """
    n_terms = len(glossary) if glossary else 0

    # Sin glosario o sin sources, la métrica no aplica
    if not glossary or not sources:
        return {
            "glossary_adherence":  None,
            "n_opportunities":     0,
            "n_applied":           0,
            "n_terms_in_glossary": n_terms,
            "missed_terms":        [],
        }

    if len(predictions) != len(sources):
        raise ValueError(
            f"predictions ({len(predictions)}) y sources ({len(sources)}) "
            "deben tener la misma longitud."
        )

    n_opportunities = 0
    n_applied       = 0
    missed: list[dict] = []

    for idx, (src, pred) in enumerate(zip(sources, predictions), start=1):
        src_low  = src.lower()
        pred_low = pred.lower()

        for term in glossary:
            term_src = term["source"].lower()
            term_tgt = term["target"].lower()

            if term_src in src_low:
                n_opportunities += 1
                if term_tgt in pred_low:
                    n_applied += 1
                else:
                    missed.append({
                        "cue_idx":   idx,
                        "source":    term["source"],
                        "expected":  term["target"],
                        "got_pred":  pred,
                    })

    if n_opportunities == 0:
        adherence = None   # ningún término del glosario aparece en el corpus
    else:
        adherence = round(n_applied / n_opportunities * 100, 2)

    return {
        "glossary_adherence":  adherence,
        "n_opportunities":     n_opportunities,
        "n_applied":           n_applied,
        "n_terms_in_glossary": n_terms,
        "missed_terms":        missed,
    }
