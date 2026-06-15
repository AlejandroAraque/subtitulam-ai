"""Tests de ajustar_cpl_optimo — el post-proceso CPL de cada cue traducido."""
from app.utils.text_utils import ajustar_cpl_optimo


def test_linea_corta_queda_intacta():
    assert ajustar_cpl_optimo("Hola mundo", max_cpl=38) == "Hola mundo"


def test_multilinea_que_cumple_queda_intacta():
    texto = "Primera línea corta\nSegunda también"
    assert ajustar_cpl_optimo(texto, max_cpl=38) == texto


def test_texto_largo_se_acerca_al_limite():
    # 79 chars en una línea: el ajuste no garantiza ≤38 (balance a 2 líneas
    # da ~40 chars c/u), pero SÍ debe mejorar respecto al original.
    largo = ("palabra " * 10).strip()
    out = ajustar_cpl_optimo(largo, max_cpl=38)
    lineas_out = out.split("\n")
    assert len(lineas_out) > 1, "el texto debería partirse en varias líneas"
    assert max(len(ln) for ln in lineas_out) < len(largo), \
        "la línea más larga del ajuste debería ser menor que el original"


def test_maximo_dos_lineas():
    muy_largo = ("palabra " * 30).strip()  # ~240 chars: no cabe en 2x38
    out = ajustar_cpl_optimo(muy_largo, max_cpl=38)
    assert len(out.split("\n")) <= 2


def test_palabra_irrompible_no_explota():
    # break_long_words=False: una "palabra" más larga que el límite
    # no se trocea — el ajuste se descarta si no mejora.
    palabra = "x" * 50
    out = ajustar_cpl_optimo(palabra, max_cpl=38)
    assert palabra in out
