"""Tests de clean_title — la limpieza de nombre de archivo para auto-context."""
from app.services.context_service import clean_title


def test_separadores_y_extension():
    assert clean_title("jakobs.ross.2024.srt") == "jakobs ross 2024"


def test_camelcase_no_se_separa():
    # Documentado en el docstring: no hay split camelCase a propósito
    assert clean_title("BreakingBad_S01E01.srt") == "BreakingBad S01E01"


def test_brackets_se_eliminan():
    assert clean_title("OPPENHEIMER [BluRay].srt") == "OPPENHEIMER"


def test_guiones_multiples():
    assert clean_title("CANDY-BAR_EN-Subtitles.srt") == "CANDY BAR EN Subtitles"


def test_solo_extension_devuelve_vacio():
    assert clean_title(".srt") == ""
