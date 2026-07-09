"""Tests del segmentador español — casos tomados del informe del revisor
profesional (2026-07-07), imágenes 2, 6 y 14."""
from app.utils.text_utils import _NO_CORTAR_TRAS, _palabra_final, ajustar_cpl_optimo


def _lineas(out: str) -> list[str]:
    return out.split("\n")


def _sin_funcional_colgando(out: str) -> bool:
    lineas = _lineas(out)
    if len(lineas) < 2:
        return True
    return _palabra_final(lineas[0]) not in _NO_CORTAR_TRAS


# ── Casos literales del informe ──────────────────────────────────────────

def test_informe_img6_cabe_en_una_linea_va_en_una():
    # "Sí. Dormiré aquí / un rato." usaba 2 líneas sin necesidad (25 chars)
    out = ajustar_cpl_optimo("Sí. Dormiré aquí\nun rato.", max_cpl=38)
    assert out == "Sí. Dormiré aquí un rato."


def test_informe_img14_no_cortar_tras_preposicion():
    # "¿Cuánto tiempo llevas viviendo en / Alemania?" cortaba tras "en"
    out = ajustar_cpl_optimo("¿Cuánto tiempo llevas viviendo en Alemania?", max_cpl=38)
    assert len(_lineas(out)) == 2
    assert _sin_funcional_colgando(out)
    assert all(len(ln) <= 38 for ln in _lineas(out))


def test_informe_img2_no_cortar_tras_articulo():
    # "Una celebración que anima a la / gente," cortaba tras "la"
    out = ajustar_cpl_optimo("Una celebración que anima a la gente,", max_cpl=30)
    assert _sin_funcional_colgando(out)


# ── Normas generales ─────────────────────────────────────────────────────

def test_corto_queda_en_una_linea():
    assert ajustar_cpl_optimo("Hola, ¿qué tal?", max_cpl=38) == "Hola, ¿qué tal?"


def test_dialogo_con_guiones_es_intocable():
    dialogo = "-Sí, yo también.\n-Sí."
    assert ajustar_cpl_optimo(dialogo, max_cpl=38) == dialogo


def test_prefiere_cortar_tras_puntuacion():
    out = ajustar_cpl_optimo(
        "No lo sé, la verdad. Pregúntale a tu madre mejor.", max_cpl=38,
    )
    lineas = _lineas(out)
    assert len(lineas) == 2
    # El corte natural es tras el punto: "No lo sé, la verdad."
    assert lineas[0].endswith(".")


def test_no_separa_auxiliar_de_participio():
    out = ajustar_cpl_optimo(
        "Los invitados de la boda ya han llegado al restaurante.", max_cpl=38,
    )
    assert _sin_funcional_colgando(out)  # "han" no puede cerrar la línea 1


def test_ambas_lineas_dentro_del_limite_cuando_es_posible():
    out = ajustar_cpl_optimo(
        "El tren con destino a Barcelona efectuará su salida en breve.",
        max_cpl=38,
    )
    assert all(len(ln) <= 38 for ln in _lineas(out))
    assert _sin_funcional_colgando(out)


def test_texto_imposible_devuelve_mal_menor_sin_explotar():
    palabra = "x" * 60  # irrompible: excede cualquier CPL
    out = ajustar_cpl_optimo(palabra, max_cpl=38)
    assert palabra in out  # no se trocea una palabra


def test_max_dos_lineas_siempre():
    largo = ("palabra " * 30).strip()
    out = ajustar_cpl_optimo(largo, max_cpl=38)
    assert len(_lineas(out)) <= 2


# ── Fixes de la auditoría v3.7 (casos verificados ejecutando el código) ──

def test_funcional_con_puntuacion_fuerte_no_veta_el_corte():
    # "más." cierra oración: cortar ahí es el corte ideal, no un "más"
    # colgando. Antes el -500 se aplicaba tras rstrip de la puntuación.
    out = ajustar_cpl_optimo(
        "No puedo más. Vámonos ya de una vez todos juntos.", max_cpl=38,
    )
    assert _lineas(out)[0] == "No puedo más."


def test_funcional_con_interrogacion_no_veta_el_corte():
    out = ajustar_cpl_optimo(
        "Vienes con nosotros o no? Pues dime algo entonces.", max_cpl=38,
    )
    assert _lineas(out)[0] == "Vienes con nosotros o no?"


def test_dialogo_con_guion_solo_en_segundo_hablante_es_intocable():
    # Muchos SRT ingleses solo marcan al 2º hablante: fusionarlo mezcla
    # dos hablantes en una línea (alteración semántica del diálogo).
    dialogo = "Sí.\n-Pero entonces cuéntame qué pasó ayer por la noche en la fiesta."
    assert ajustar_cpl_optimo(dialogo, max_cpl=38) == dialogo


def test_tags_no_cuentan_para_el_cpl():
    # 36 chars visibles: cabe en una línea aunque el len crudo sea 43
    out = ajustar_cpl_optimo("<i>Cinema Paradiso</i> es una gran película", max_cpl=38)
    assert len(_lineas(out)) == 1


def test_tag_font_con_atributos_no_se_parte_por_dentro():
    out = ajustar_cpl_optimo(
        'Ella dijo que vendría <font color="red">mañana temprano</font> a casa',
        max_cpl=38,
    )
    assert '<font color="red">' in out  # el tag sobrevive entero
    assert len(_lineas(out)) == 2


def test_no_separa_estar_del_gerundio():
    out = ajustar_cpl_optimo(
        "María y su hermana pequeña están esperando en la puerta.", max_cpl=38,
    )
    assert _sin_funcional_colgando(out)  # "están" no puede cerrar la línea 1


def test_relativos_y_determinantes_en_lista():
    for palabra in ("cuya", "quien", "cada", "ningún", "está", "fue"):
        assert palabra in _NO_CORTAR_TRAS
