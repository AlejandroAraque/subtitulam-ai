"""Tests del control de velocidad de lectura (CPS) — A2 del backlog,
respuesta al informe del revisor (imagen 14: CPS >17 no controlado)."""
from app.services.translation_service import CPS_LIMIT, _char_budget
from app.utils.text_utils import visible_chars


def test_budget_limitado_por_velocidad_de_lectura():
    # Cue de 1 s: aunque quepan 76 chars (2×38), a 17 CPS solo 17
    assert _char_budget(1.0, 38) == 17


def test_budget_limitado_por_lineas_fisicas():
    # Cue de 10 s: la velocidad daría 170, pero 2 líneas de 38 = 76 tope
    assert _char_budget(10.0, 38) == 76


def test_budget_nunca_cero():
    assert _char_budget(0.0, 38) >= 1


def test_cps_limit_es_17():
    assert CPS_LIMIT == 17.0


def test_visible_chars_ignora_saltos_y_tags():
    assert visible_chars("Hola\nmundo") == len("Hola mundo")
    assert visible_chars("<i>Hola</i> mundo") == len("Hola mundo")


def test_visible_chars_cue_normal():
    # "¿Por unas palomitas y una chocolatina?" = 38 chars
    assert visible_chars("¿Por unas palomitas\ny una chocolatina?") == 38
