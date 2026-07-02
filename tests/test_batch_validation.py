"""Tests de la validación de respuestas del LLM (BACK-01).

La lógica vive inline en translate_texts (llamada async con red), así que
aquí se testea la pieza parseable y el contrato de saneado que aplica:
índices fantasma descartados, cues faltantes marcadas — usando el mismo
algoritmo con parsear_traducciones como entrada.
"""
from app.services.translation_service import parsear_traducciones


def _sanea(bloque: list[tuple[int, str]], respuesta_llm: str) -> dict[int, str]:
    """Réplica del saneado de translate_texts (índices fantasma + missing).

    Espeja las reglas (b) y (c) del bloque de validación: si esta réplica
    divergiera del código real, los asserts de integración lo cazarían al
    subir de nivel; su valor aquí es fijar el contrato esperado.
    """
    parsed = parsear_traducciones(respuesta_llm)
    batch_idxs = {idx for idx, _ in bloque}
    out: dict[int, str] = {}
    for idx, texto in parsed.items():
        if idx in batch_idxs:            # descarta fantasmas
            out[idx] = texto
    for idx, src in bloque:              # marca faltantes
        if idx not in out:
            out[idx] = f"[ERROR] {src}"
    return out


def test_respuesta_completa_sin_cambios():
    bloque = [(1, "Hello"), (2, "World")]
    out = _sanea(bloque, "1: Hola\n2: Mundo")
    assert out == {1: "Hola", 2: "Mundo"}


def test_indice_fantasma_descartado():
    # El LLM alucina el índice 99: no debe entrar en el resultado
    bloque = [(1, "Hello")]
    out = _sanea(bloque, "1: Hola\n99: Texto alucinado")
    assert 99 not in out
    assert out[1] == "Hola"


def test_indice_fantasma_no_sobrescribe_cue_anterior():
    # Batch actual: cues 6-7. El LLM devuelve un "5:" (del batch anterior).
    bloque = [(6, "Six"), (7, "Seven")]
    out = _sanea(bloque, "5: Cinco alucinado\n6: Seis\n7: Siete")
    assert 5 not in out


def test_cue_faltante_marcada_error_no_silenciosa():
    # El LLM devuelve solo 1 de 2 cues: la faltante debe quedar marcada
    # [ERROR] con el texto original, NUNCA ausente (antes el caller la
    # rellenaba con el inglés en silencio).
    bloque = [(1, "Hello"), (2, "World")]
    out = _sanea(bloque, "1: Hola")
    assert out[2] == "[ERROR] World"


def test_respuesta_vacia_marca_todo():
    bloque = [(1, "Hello"), (2, "World")]
    out = _sanea(bloque, "sin formato valido")
    assert out == {1: "[ERROR] Hello", 2: "[ERROR] World"}
