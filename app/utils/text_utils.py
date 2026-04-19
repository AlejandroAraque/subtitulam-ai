import textwrap

def ajustar_cpl_optimo(texto: str, max_cpl: int = 38) -> str:
    """
    Ajusta un subtítulo para cumplir con los Caracteres Por Línea (CPL) de manera óptima.
    Si el ajuste no mejora la situación, mantiene las líneas originales.
    """
    lineas = texto.split("\n")
    cpl_original = [len(l) for l in lineas]

    # Caso 1: Todas cumplen
    if all(l <= max_cpl for l in cpl_original):
        return texto

    # Intentar ajuste
    texto_unido = " ".join(l.strip() for l in lineas)
    nuevas_lineas = textwrap.wrap(
        texto_unido,
        width=max_cpl,
        break_long_words=False
    )

    # Balancear en máximo 2 líneas
    if len(nuevas_lineas) > 2:
        mitad = len(nuevas_lineas) // 2
        nuevas_lineas = [
            " ".join(nuevas_lineas[:mitad]).strip(),
            " ".join(nuevas_lineas[mitad:]).strip()
        ]

    cpl_ajustado = [len(l) for l in nuevas_lineas]

    # Solo aplicar ajuste si mejora la situación
    if all(l <= max_cpl for l in cpl_ajustado) or max(cpl_ajustado) < max(cpl_original):
        return "\n".join(nuevas_lineas)
    
    return texto