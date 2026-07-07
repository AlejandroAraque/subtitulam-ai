"""Utilidades de texto para subtitulado profesional.

La pieza central es `ajustar_cpl_optimo`, que aplica las normas de
segmentación españolas (UNE 153010 + convenciones del sector) señaladas
por el revisor profesional del piloto (informe 2026-07-07):

  - Si el texto cabe en UNA línea, va en una línea (no reproducir la
    segmentación de dos líneas del SRT inglés).
  - Máximo dos líneas.
  - El corte de línea NO puede dejar colgando al final de la primera
    línea una palabra funcional: preposición, artículo, conjunción,
    pronombre átono, auxiliar de "haber", negación… (era el fallo más
    repetido: "que anima a la / gente", "viviendo en / Alemania").
  - Preferir cortes tras signos de puntuación y líneas equilibradas.
  - Las líneas de diálogo con guion ("-Sí. / -No.") son intocables:
    cada hablante conserva su línea.
"""

import re

# Palabras que NO pueden quedar al final de la primera línea (el corte
# las separaría de lo que introducen). Minúsculas; la comparación ignora
# mayúsculas del inicio de frase.
_NO_CORTAR_TRAS = {
    # artículos y contracciones
    "el", "la", "los", "las", "un", "una", "unos", "unas", "al", "del",
    # preposiciones
    "a", "ante", "bajo", "con", "contra", "de", "desde", "durante", "en",
    "entre", "hacia", "hasta", "mediante", "para", "por", "según", "sin",
    "sobre", "tras",
    # conjunciones y relativos frecuentes
    "y", "e", "ni", "o", "u", "pero", "sino", "que", "como", "si",
    "aunque", "porque", "pues", "cuando", "mientras", "donde",
    # posesivos y demostrativos antepuestos
    "mi", "tu", "su", "mis", "tus", "sus", "nuestro", "nuestra",
    "nuestros", "nuestras", "vuestro", "vuestra", "vuestros", "vuestras",
    "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
    "aquel", "aquella", "aquellos", "aquellas",
    # pronombres átonos y negación (preceden al verbo)
    "me", "te", "se", "le", "les", "lo", "nos", "os", "no",
    # auxiliares de tiempos compuestos (no separar "ha comido")
    "he", "has", "ha", "hemos", "habéis", "han",
    "había", "habías", "habíamos", "habíais", "habían",
    "habré", "habrás", "habrá", "habremos", "habréis", "habrán",
    # intensificadores que modifican lo siguiente
    "muy", "tan", "más", "menos",
}

_PUNTUACION_FIN = (".", ",", ";", ":", "?", "!", "…", "—", "-")


def _es_dialogo_multilinea(texto: str) -> bool:
    """True si el cue son líneas de diálogo con guion (cada hablante una
    línea): esa segmentación es semántica y no debe tocarse."""
    lineas = [ln.strip() for ln in texto.split("\n") if ln.strip()]
    if len(lineas) < 2:
        return False
    con_guion = sum(1 for ln in lineas if ln.startswith(("-", "–", "—")))
    return con_guion >= 2


def _palabra_final(linea: str) -> str:
    """Última palabra de una línea, sin puntuación ni tags, en minúsculas."""
    limpia = re.sub(r"<[^>]+>", "", linea).strip()
    if not limpia:
        return ""
    palabra = limpia.split()[-1]
    return palabra.strip("¿¡\"'«»()[]").rstrip(".,;:?!…").lower()


def _puntuar_corte(linea1: str, linea2: str, max_cpl: int) -> float:
    """Puntuación de un candidato a corte (mayor = mejor)."""
    l1, l2 = len(linea1), len(linea2)
    score = 0.0

    # Restricción casi dura: ambas líneas dentro del límite
    if l1 > max_cpl:
        score -= 1000 + (l1 - max_cpl) * 10
    if l2 > max_cpl:
        score -= 1000 + (l2 - max_cpl) * 10

    # Prohibición lingüística: no dejar palabra funcional colgando
    if _palabra_final(linea1) in _NO_CORTAR_TRAS:
        score -= 500

    # Bonus fuerte: cortar justo después de puntuación (pausa natural)
    if linea1.rstrip().endswith(_PUNTUACION_FIN):
        score += 60

    # Equilibrio entre líneas (evitar 35/4)
    score -= abs(l1 - l2) * 1.0

    return score


def segmentar_subtitulo(texto: str, max_cpl: int = 38) -> str:
    """Re-segmenta el texto de un cue según las normas españolas.

    Devuelve 1 o 2 líneas. Si ni el mejor corte posible respeta el CPL
    (texto demasiado largo), devuelve el mal menor — la corrección de
    longitud es trabajo de la condensación, no del segmentador.
    """
    # Diálogos con guion: intocables
    if _es_dialogo_multilinea(texto):
        return texto

    plano = " ".join(texto.split())
    if not plano:
        return texto

    # Cabe en una línea → una línea (norma: no usar dos sin necesidad)
    if len(plano) <= max_cpl:
        return plano

    # Buscar el mejor punto de corte entre palabras
    palabras = plano.split(" ")
    if len(palabras) < 2:
        return plano  # una sola palabra gigante: nada que cortar

    mejor: tuple[float, str] | None = None
    for i in range(1, len(palabras)):
        l1 = " ".join(palabras[:i])
        l2 = " ".join(palabras[i:])
        s = _puntuar_corte(l1, l2, max_cpl)
        if mejor is None or s > mejor[0]:
            mejor = (s, f"{l1}\n{l2}")
    return mejor[1]


def ajustar_cpl_optimo(texto: str, max_cpl: int = 38) -> str:
    """Ajusta un subtítulo a las normas de líneas del subtitulado.

    Desde v3.6 delega en `segmentar_subtitulo`: siempre re-segmenta con
    reglas lingüísticas (antes solo actuaba si se excedía el CPL, y el
    corte era `textwrap` puro, que dejaba preposiciones y artículos
    colgando a final de línea).
    """
    return segmentar_subtitulo(texto, max_cpl=max_cpl)
