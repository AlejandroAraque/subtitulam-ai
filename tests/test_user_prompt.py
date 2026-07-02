"""Tests de build_user_prompt — incluida la inyección de contexto RAG (v3.5.1)."""
from app.services.translation_service import build_user_prompt


def test_batch_solo():
    out = build_user_prompt([(1, "Hello"), (2, "World")])
    assert "AHORA TRADUCE" in out
    assert "1: Hello" in out and "2: World" in out
    assert "EJEMPLOS PREVIOS" not in out


def test_ejemplo_sin_contexto_no_pinta_marco():
    out = build_user_prompt(
        [(1, "Hi")],
        rag_examples=[{"source_text": "Hello", "target_text": "Hola"}],
    )
    assert 'EN: "Hello"' in out and 'ES: "Hola"' in out
    assert "[obra:" not in out and "antes:" not in out


def test_ejemplo_con_contexto_y_vecina():
    out = build_user_prompt(
        [(1, "You're getting them!")],
        rag_examples=[{
            "source_text": "You're getting them!",
            "target_text": "¡Te las llevas!",
            "context": "Candy Bar — comedia en un cine",
            "prev_text": "Actually, make it a medium.",
        }],
    )
    assert "[obra: Candy Bar — comedia en un cine" in out
    assert 'antes: "Actually, make it a medium."' in out
    # El marco va ANTES del par EN/ES
    assert out.index("[obra:") < out.index('EN: "You\'re getting them!"')


def test_contexto_largo_se_trunca():
    out = build_user_prompt(
        [(1, "Hi")],
        rag_examples=[{
            "source_text": "Hello", "target_text": "Hola",
            "context": "x" * 500,
            "prev_text": "y" * 500,
        }],
    )
    # obra truncada a 100 chars, prev a 60
    assert "x" * 101 not in out
    assert "y" * 61 not in out
