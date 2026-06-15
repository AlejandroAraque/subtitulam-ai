"""Tests de las métricas de eval/ — las que generan las cifras del TFM."""
import pytest

from eval.metrics import bleu, chrf, cpl, glossary

# ── CPL ──────────────────────────────────────────────────────────────────

def test_cpl_mitad_de_lineas_sobre_limite():
    r = cpl.compute(["corta", "x" * 50], cpl_limit=42)
    assert r["cpl_compliance"] == 50.0
    assert r["n_lines_total"] == 2
    assert r["n_lines_over"] == 1
    assert r["max_line_len"] == 50


def test_cpl_todas_cumplen():
    r = cpl.compute(["hola", "mundo\ncorto"], cpl_limit=42)
    assert r["cpl_compliance"] == 100.0
    assert r["n_lines_total"] == 3  # multilinea cuenta por línea visible


def test_cpl_sin_lineas():
    r = cpl.compute([""], cpl_limit=42)
    assert r["cpl_compliance"] == 0.0
    assert r["n_lines_total"] == 0


def test_cpl_lineas_vacias_no_cuentan():
    r = cpl.compute(["hola\n\n\nmundo"], cpl_limit=42)
    assert r["n_lines_total"] == 2


# ── BLEU / chrF (sanity sobre sacrebleu) ─────────────────────────────────

def test_bleu_identidad_es_100():
    r = bleu.compute(["Hola mundo esto es una frase"],
                     ["Hola mundo esto es una frase"])
    assert r["bleu"] == pytest.approx(100.0)


def test_bleu_disjunto_es_bajo():
    r = bleu.compute(["aaa bbb ccc"], ["xxx yyy zzz"])
    assert r["bleu"] < 5.0


def test_chrf_identidad_es_100():
    r = chrf.compute(["Hola mundo"], ["Hola mundo"])
    assert r["chrf"] == pytest.approx(100.0)


# ── Glosario (incluye la regresión del bug de substring) ────────────────

def test_glossary_substring_no_cuenta_como_oportunidad():
    # REGRESIÓN QA-09: "art" NO debe matchear dentro de "Start the car"
    r = glossary.compute(
        ["Arranca el coche"],
        sources=["Start the car"],
        glossary=[{"source": "art", "target": "arte"}],
    )
    assert r["n_opportunities"] == 0
    assert r["glossary_adherence"] is None


def test_glossary_termino_aplicado():
    r = glossary.compute(
        ["Es una chocolatina"],
        sources=["A choc-top please"],
        glossary=[{"source": "choc-top", "target": "chocolatina"}],
    )
    assert r["n_opportunities"] == 1
    assert r["n_applied"] == 1
    assert r["glossary_adherence"] == 100.0


def test_glossary_termino_no_aplicado_queda_en_missed():
    r = glossary.compute(
        ["Es un choc-top"],  # el sistema no tradujo el término
        sources=["A choc-top please"],
        glossary=[{"source": "choc-top", "target": "chocolatina"}],
    )
    assert r["n_applied"] == 0
    assert len(r["missed_terms"]) == 1
    assert r["missed_terms"][0]["source"] == "choc-top"


def test_glossary_sin_glosario_no_aplica():
    r = glossary.compute(["hola"], sources=["hello"], glossary=[])
    assert r["glossary_adherence"] is None


def test_glossary_longitudes_distintas_lanza():
    with pytest.raises(ValueError):
        glossary.compute(
            ["uno"],
            sources=["one", "two"],
            glossary=[{"source": "a", "target": "b"}],
        )


def test_glossary_case_insensitive():
    r = glossary.compute(
        ["Una CHOCOLATINA"],
        sources=["A Choc-Top"],
        glossary=[{"source": "choc-top", "target": "chocolatina"}],
    )
    assert r["n_applied"] == 1
