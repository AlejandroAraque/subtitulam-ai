"""Tests de _format_glossary_block — la inyección del glosario en el prompt."""
from app.services.translation_service import _format_glossary_block


def test_vacio_devuelve_string_vacio():
    assert _format_glossary_block([]) == ""


def test_termino_aparece_con_flecha_y_categoria():
    block = _format_glossary_block([
        {"source": "choc-top", "target": "chocolatina", "category": "término"},
    ])
    assert '"choc-top" → "chocolatina"' in block
    assert "[término]" in block
    assert "GLOSARIO OBLIGATORIO" in block


def test_nota_se_incluye_tras_em_dash():
    block = _format_glossary_block([
        {"source": "loo", "target": "baño", "category": "slang", "note": "UK informal"},
    ])
    assert "— UK informal" in block


def test_orden_deterministico_por_categoria_y_source():
    terms = [
        {"source": "zebra", "target": "cebra", "category": "término"},
        {"source": "apple", "target": "manzana", "category": "término"},
        {"source": "BBC", "target": "BBC", "category": "acrónimo"},
    ]
    block = _format_glossary_block(terms)
    # acrónimo < término alfabéticamente; dentro de término: apple < zebra
    assert block.index("BBC") < block.index("apple") < block.index("zebra")
    # Mismo input en otro orden produce el mismo bloque (reproducibilidad)
    assert block == _format_glossary_block(list(reversed(terms)))


def test_termino_sin_source_o_target_se_descarta():
    block = _format_glossary_block([
        {"source": "", "target": "algo", "category": "término"},
        {"source": "ok", "target": "vale", "category": "término"},
    ])
    assert '"ok" → "vale"' in block
    assert '"" →' not in block
