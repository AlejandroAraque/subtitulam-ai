"""Tests de la condensación CPS (_condensar_violaciones).

La auditoría v3.7 encontró que esta función no tenía ni un test y
acumulaba 3 bugs verificados: truncado silencioso por max_tokens,
diálogos aplanados (dos hablantes en una línea) y duraciones negativas
que forzaban presupuestos de 1 carácter.
"""
import asyncio
from types import SimpleNamespace

from app.services import translation_service as ts


def _resp(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason=finish_reason,
        )],
        usage=None,
    )


class _FakeOpenAI:
    """Cliente mínimo: devuelve respuestas preparadas y guarda los prompts."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.prompts: list[str] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1]["content"])
        return self.respuestas.pop(0)


def _condensar(translated, durations, cpl=38):
    return asyncio.run(
        ts._condensar_violaciones(translated, durations, cpl, "es", "")
    )


LARGO = "Este es un texto larguísimo que no cabe de ninguna manera en el hueco."


# ── _contar_violaciones ───────────────────────────────────────────────────

def test_contar_violaciones_detecta_exceso_e_ignora_error():
    translated = {1: "x" * 40, 2: "corto", 3: "[ERROR] " + "y" * 40}
    durations = {1: 1.0, 2: 1.0, 3: 1.0}  # presupuesto 17
    infractores = ts._contar_violaciones(translated, durations, 38)
    assert [i[0] for i in infractores] == [1]


def test_duracion_negativa_no_es_infractora():
    # end<start (timecodes corruptos) daba presupuesto [≤1] y el LLM
    # intentaba comprimir un subtítulo a 1 carácter.
    assert ts._contar_violaciones({1: LARGO}, {1: -2.0}, 38) == []


# ── Aceptación / rechazo ─────────────────────────────────────────────────

def test_acepta_reescritura_mas_corta(monkeypatch):
    fake = _FakeOpenAI([_resp("1: Texto corto.")])
    monkeypatch.setattr(ts, "get_openai", lambda: fake)
    translated = {1: LARGO}
    n = _condensar(translated, {1: 1.0})
    assert n == 1
    assert translated[1] == "Texto corto."


def test_rechaza_si_no_recorta(monkeypatch):
    fake = _FakeOpenAI([_resp(f"1: {LARGO}")])
    monkeypatch.setattr(ts, "get_openai", lambda: fake)
    translated = {1: LARGO}
    n = _condensar(translated, {1: 1.0})
    assert n == 0
    assert translated[1] == LARGO


# ── Diálogos ─────────────────────────────────────────────────────────────

def test_dialogo_no_se_fusiona_en_una_linea(monkeypatch):
    original = (
        "-Ven aquí ahora mismo, por favor, te lo pido.\n"
        "-No pienso ir, de verdad te lo digo."
    )
    fake = _FakeOpenAI([_resp("1: -Ven ya. -No.")])
    monkeypatch.setattr(ts, "get_openai", lambda: fake)
    translated = {1: original}
    n = _condensar(translated, {1: 1.0})
    assert n == 1
    # El LLM lo devolvió aplanado: se re-parte por hablantes
    assert translated[1] == "-Ven ya.\n-No."
    # Y el prompt conservó los saltos de línea del diálogo original
    assert original in fake.prompts[0]


def test_dialogo_irreconstruible_se_rechaza(monkeypatch):
    original = "-Ven aquí ahora mismo, por favor.\n-No pienso ir, de verdad."
    # Sin guiones: no hay forma segura de re-partir hablantes
    fake = _FakeOpenAI([_resp("1: Ven ya y no discutas.")])
    monkeypatch.setattr(ts, "get_openai", lambda: fake)
    translated = {1: original}
    n = _condensar(translated, {1: 1.0})
    assert n == 0
    assert translated[1] == original


# ── Truncado por max_tokens ──────────────────────────────────────────────

def test_truncado_descarta_la_ultima_cue(monkeypatch):
    t1, t2 = LARGO, LARGO.replace("texto", "asunto")
    fake = _FakeOpenAI([_resp("1: Uno corto.\n2: Dos cor", finish_reason="length")])
    monkeypatch.setattr(ts, "get_openai", lambda: fake)
    translated = {1: t1, 2: t2}
    n = _condensar(translated, {1: 1.0, 2: 1.0})
    assert n == 1
    assert translated[1] == "Uno corto."
    assert translated[2] == t2  # la cue cortada a mitad NO se escribe


# ── Troceo en grupos ─────────────────────────────────────────────────────

def test_trocea_en_grupos_de_20(monkeypatch):
    fake = _FakeOpenAI([_resp(""), _resp(""), _resp("")])
    monkeypatch.setattr(ts, "get_openai", lambda: fake)
    translated = {i: f"{i} — {LARGO}" for i in range(1, 46)}  # 45 infractores
    durations = {i: 1.0 for i in range(1, 46)}
    _condensar(translated, durations)
    assert len(fake.prompts) == 3  # 20 + 20 + 5, no un batch único


# ── Diálogo aplanado: helper ─────────────────────────────────────────────

def test_repartir_dialogo():
    assert ts._repartir_dialogo("-Hola. -Adiós.") == "-Hola.\n-Adiós."
    assert ts._repartir_dialogo("Sin guiones aquí.") == "Sin guiones aquí."
    ya_multilinea = "-Hola.\n-Adiós."
    assert ts._repartir_dialogo(ya_multilinea) == ya_multilinea
