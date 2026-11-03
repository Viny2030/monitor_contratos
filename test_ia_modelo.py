"""Tests de agentic_ai._pedir_a_claude con Claude Sonnet 5.5 (sin red: cliente simulado)."""
from types import SimpleNamespace

import agentic_ai


class _Cliente:
    def __init__(self, resp):
        self.resp = resp
        self.kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def _resp(bloques, stop="end_turn"):
    return SimpleNamespace(content=bloques, stop_reason=stop)


def _texto(t):
    return SimpleNamespace(type="text", text=t)


def test_modelo_por_defecto_es_sonnet_5_5():
    # Se lee del código: ANTHROPIC_MODEL en el entorno puede pisar el valor importado.
    with open(agentic_ai.__file__, encoding="utf-8") as f:
        assert 'os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")' in f.read()


def test_apaga_el_razonamiento_y_ignora_bloques_que_no_son_texto(monkeypatch):
    cli = _Cliente(_resp([SimpleNamespace(type="thinking", thinking=""), _texto(" Explicación. ")]))
    monkeypatch.setattr(agentic_ai, "_client", cli)
    monkeypatch.setattr(agentic_ai, "MODEL", "claude-sonnet-5-5")
    r = agentic_ai._pedir_a_claude("sistema", "pregunta")
    assert r == {"disponible": True, "explicacion": "Explicación."}
    assert cli.kwargs["thinking"] == {"type": "between_tools"}


def test_otro_modelo_no_recibe_thinking(monkeypatch):
    cli = _Cliente(_resp([_texto("ok")]))
    monkeypatch.setattr(agentic_ai, "_client", cli)
    monkeypatch.setattr(agentic_ai, "MODEL", "claude-sonnet-4-5")
    assert agentic_ai._pedir_a_claude("s", "p")["disponible"] is True
    assert "thinking" not in cli.kwargs


def test_respuesta_sin_texto_o_rechazo_no_se_muestra_como_explicacion(monkeypatch):
    monkeypatch.setattr(agentic_ai, "MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(agentic_ai, "_client", _Cliente(_resp([], stop="max_tokens")))
    r = agentic_ai._pedir_a_claude("s", "p")
    assert r["disponible"] is False and "sin texto" in r["motivo"]
    monkeypatch.setattr(agentic_ai, "_client", _Cliente(_resp([_texto("x")], stop="refusal")))
    r = agentic_ai._pedir_a_claude("s", "p")
    assert r["disponible"] is False and "declinó" in r["motivo"]
