"""Tests del parser de la respuesta del LLM (formato 'N: texto')."""
from app.services.translation_service import parsear_traducciones


def test_caso_basico():
    out = parsear_traducciones("5: Hola\n6: Adiós")
    assert out == {5: "Hola", 6: "Adiós"}


def test_multilinea_se_une_al_indice_anterior():
    out = parsear_traducciones("5: Primera línea\nsegunda del mismo cue\n6: Otro")
    assert out == {5: "Primera línea\nsegunda del mismo cue", 6: "Otro"}


def test_texto_sin_indice_devuelve_vacio():
    assert parsear_traducciones("texto suelto sin formato") == {}


def test_indice_con_espacios():
    out = parsear_traducciones("  12 :  Con espacios  ")
    assert out == {12: "Con espacios"}


def test_lineas_vacias_se_ignoran():
    out = parsear_traducciones("5: Hola\n\n\n6: Adiós")
    assert out == {5: "Hola", 6: "Adiós"}


def test_indice_repetido_ultimo_gana():
    out = parsear_traducciones("5: Primero\n5: Segundo")
    assert out == {5: "Segundo"}
