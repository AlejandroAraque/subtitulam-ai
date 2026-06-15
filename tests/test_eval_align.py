"""Tests del alineador temporal de eval_against_human — del que dependen
las conclusiones del A/B contra traducción humana profesional."""
import pytest

from eval.eval_against_human import (
    Cue,
    _ts_to_seconds,
    align_by_overlap,
    normalized_diff,
    parse_srt,
)

# ── Timestamps ───────────────────────────────────────────────────────────

def test_ts_basico():
    assert _ts_to_seconds("00:01:02,500") == 62.5


def test_ts_con_punto_tambien_vale():
    assert _ts_to_seconds("00:01:02.500") == 62.5


def test_ts_horas():
    assert _ts_to_seconds("01:00:00,000") == 3600.0


def test_ts_malformado_lanza():
    with pytest.raises(ValueError):
        _ts_to_seconds("1:2")


# ── Alineación por solapamiento ──────────────────────────────────────────

def _cue(idx: int, start: float, end: float, text: str = "x") -> Cue:
    return Cue(idx=idx, start_s=start, end_s=end, text=text)


def test_solapamiento_total_alinea():
    en = [_cue(1, 0.0, 2.0, "Hello")]
    es = [_cue(1, 0.0, 2.0, "Hola")]
    mapping = align_by_overlap(en, es)
    assert mapping[1].text == "Hola"


def test_timecodes_desplazados_alinean_por_solape_parcial():
    # El humano reajusta timecodes: solape parcial pero claro
    en = [_cue(1, 10.0, 12.0, "Hello")]
    es = [_cue(1, 10.4, 12.6, "Hola")]
    mapping = align_by_overlap(en, es)
    assert 1 in mapping


def test_cue_sin_match_queda_fuera():
    # Omisión humana: ningún cue ES cerca del EN
    en = [_cue(1, 10.0, 11.0, "Um...")]
    es = [_cue(1, 50.0, 52.0, "Otra cosa")]
    mapping = align_by_overlap(en, es)
    assert 1 not in mapping


def test_fusion_humana_dos_en_un_es():
    # El humano fusiona dos cues EN en uno ES: ambos EN apuntan al mismo
    en = [_cue(1, 0.0, 2.0, "See that?"), _cue(2, 2.0, 4.0, "See all that?")]
    es = [_cue(1, 0.0, 4.0, "¿Ves todo eso?")]
    mapping = align_by_overlap(en, es)
    assert mapping[1] is mapping[2]


# ── Distancia de edición normalizada ─────────────────────────────────────

def test_diff_identico_es_cero():
    assert normalized_diff("hola", "hola") == 0.0


def test_diff_totalmente_distinto_es_uno():
    assert normalized_diff("aaaa", "zzzz") == 1.0


def test_diff_vacios():
    assert normalized_diff("", "") == 0.0
    assert normalized_diff("abc", "") == 1.0


# ── Parser SRT del eval ──────────────────────────────────────────────────

def test_parse_srt_basico(tmp_path):
    srt = tmp_path / "test.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHola\n\n"
        "2\n00:00:03,000 --> 00:00:04,500\nMundo\nsegunda línea\n",
        encoding="utf-8",
    )
    cues = parse_srt(srt)
    assert len(cues) == 2
    assert cues[0].text == "Hola"
    assert cues[1].text == "Mundo\nsegunda línea"
    assert cues[1].start_s == 3.0
    assert cues[1].end_s == 4.5


def test_parse_srt_con_bom(tmp_path):
    srt = tmp_path / "bom.srt"
    srt.write_bytes("﻿1\n00:00:01,000 --> 00:00:02,000\nHola\n".encode("utf-8"))
    cues = parse_srt(srt)
    assert len(cues) == 1
