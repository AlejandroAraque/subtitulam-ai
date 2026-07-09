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

Todas las medidas de longitud son sobre caracteres VISIBLES: los tags
HTML de subtitulado (<i>, <font…>) no cuentan para el CPL ni el CPS y
nunca se parten por dentro.
"""

import re

# Tags HTML de subtitulado. Restringido a los conocidos: un regex genérico
# <[^>]+> se tragaba texto real entre ángulos ("5 < 10 y 20 > 3") y la cue
# escapaba del control de CPS.
_TAG_RE = re.compile(r"</?(?:i|b|u|font|em|strong)(?:\s[^>]*)?>", re.IGNORECASE)

# Palabras que NO pueden quedar al final de la primera línea (el corte
# las separaría de lo que introducen). Minúsculas; la comparación ignora
# mayúsculas del inicio de frase. Si la palabra cierra oración con
# puntuación fuerte ("No puedo más."), la condición no aplica: eso lo
# resuelve _puntuar_corte.
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
    "cuyo", "cuya", "cuyos", "cuyas", "cual", "cuales", "quien", "quienes",
    # posesivos y demostrativos antepuestos
    "mi", "tu", "su", "mis", "tus", "sus", "nuestro", "nuestra",
    "nuestros", "nuestras", "vuestro", "vuestra", "vuestros", "vuestras",
    "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
    "aquel", "aquella", "aquellos", "aquellas",
    # determinantes antepuestos
    "cada", "algún", "alguna", "algunos", "algunas",
    "ningún", "ninguna", "otro", "otra", "otros", "otras",
    # pronombres átonos y negación (preceden al verbo)
    "me", "te", "se", "le", "les", "lo", "nos", "os", "no",
    # auxiliares de tiempos compuestos (no separar "ha comido")
    "he", "has", "ha", "hemos", "habéis", "han",
    "había", "habías", "habíamos", "habíais", "habían",
    "habré", "habrás", "habrá", "habremos", "habréis", "habrán",
    # perífrasis con estar (no separar "está esperando")
    "estoy", "estás", "está", "estamos", "estáis", "están",
    "estaba", "estabas", "estábamos", "estabais", "estaban",
    # ser copulativo / pasiva (no separar "fue construido", "es una…")
    "soy", "eres", "es", "somos", "sois", "son",
    "era", "eran", "fue", "fueron", "será", "serán",
    # intensificadores que modifican lo siguiente
    "muy", "tan", "más", "menos",
}

_PUNTUACION_FIN = (".", ",", ";", ":", "?", "!", "…", "—")

# Puntuación fuerte: cierra oración y desactiva la condición de "palabra
# funcional que introduce lo siguiente" ("más." ya no introduce nada).
_PUNTUACION_ORACION = (".", "?", "!", "…")


def visible_chars(texto: str) -> int:
    """Caracteres que el espectador realmente lee: sin tags HTML y
    contando el texto de todas las líneas (los saltos de línea no
    cuentan para la velocidad de lectura)."""
    limpio = _TAG_RE.sub("", texto)
    return len(limpio.replace("\n", " ").strip())


def _es_dialogo_multilinea(texto: str) -> bool:
    """True si el cue son líneas de diálogo con guion: esa segmentación es
    semántica y no debe tocarse. Basta con que una línea posterior a la
    primera empiece por guion — muchos SRT solo marcan al 2º hablante
    ("Sí.\\n-Pero entonces…")."""
    lineas = [ln.strip() for ln in texto.split("\n") if ln.strip()]
    if len(lineas) < 2:
        return False
    con_guion = sum(1 for ln in lineas if ln.startswith(("-", "–", "—")))
    if con_guion >= 2:
        return True
    return any(ln.startswith(("-", "–", "—")) for ln in lineas[1:])


def _palabra_final(linea: str) -> str:
    """Última palabra de una línea, sin puntuación ni tags, en minúsculas."""
    limpia = _TAG_RE.sub("", linea).strip()
    if not limpia:
        return ""
    palabra = limpia.split()[-1]
    return palabra.strip("¿¡\"'«»()[]").rstrip(".,;:?!…").lower()


def _puntuar_corte(linea1: str, linea2: str, max_cpl: int) -> float:
    """Puntuación de un candidato a corte (mayor = mejor).

    Las longitudes se miden en caracteres visibles (sin tags HTML):
    medir con len() crudo desdoblaba en dos líneas cues que caben
    visibles en una."""
    l1, l2 = visible_chars(linea1), visible_chars(linea2)
    score = 0.0

    # Restricción casi dura: ambas líneas dentro del límite
    if l1 > max_cpl:
        score -= 1000 + (l1 - max_cpl) * 10
    if l2 > max_cpl:
        score -= 1000 + (l2 - max_cpl) * 10

    limpia1 = _TAG_RE.sub("", linea1).rstrip()
    cierra_oracion = limpia1.endswith(_PUNTUACION_ORACION)

    # Prohibición lingüística: no dejar palabra funcional colgando.
    # Con puntuación fuerte no aplica: "No puedo más." cierra la oración,
    # cortar ahí es el corte ideal, no una preposición colgando.
    if not cierra_oracion and _palabra_final(linea1) in _NO_CORTAR_TRAS:
        score -= 500

    # Bonus fuerte: cortar justo después de puntuación (pausa natural)
    if limpia1.endswith(_PUNTUACION_FIN):
        score += 60

    # Equilibrio entre líneas (evitar 35/4)
    score -= abs(l1 - l2) * 1.0

    return score


def _proteger_tags(texto: str) -> str:
    """Sustituye los espacios INTERNOS de los tags por un centinela para
    que el split por espacios no parta '<font color="red">' por dentro."""
    return _TAG_RE.sub(lambda m: m.group(0).replace(" ", "\x00"), texto)


def _restaurar_tags(texto: str) -> str:
    return texto.replace("\x00", " ")


def segmentar_subtitulo(texto: str, max_cpl: int = 38) -> str:
    """Re-segmenta el texto de un cue según las normas españolas.

    Devuelve 1 o 2 líneas. Si ni el mejor corte posible respeta el CPL
    (texto demasiado largo), devuelve el mal menor — la corrección de
    longitud es trabajo de la condensación, no del segmentador.
    """
    # Diálogos con guion: intocables
    if _es_dialogo_multilinea(texto):
        return texto

    plano = " ".join(_proteger_tags(texto).split())
    if not plano:
        return texto

    # Cabe en una línea → una línea (norma: no usar dos sin necesidad)
    if visible_chars(_restaurar_tags(plano)) <= max_cpl:
        return _restaurar_tags(plano)

    # Buscar el mejor punto de corte entre palabras
    palabras = plano.split(" ")
    if len(palabras) < 2:
        return _restaurar_tags(plano)  # una sola palabra gigante: nada que cortar

    mejor: tuple[float, str] | None = None
    for i in range(1, len(palabras)):
        l1 = _restaurar_tags(" ".join(palabras[:i]))
        l2 = _restaurar_tags(" ".join(palabras[i:]))
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
