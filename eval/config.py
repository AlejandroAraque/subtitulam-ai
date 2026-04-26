"""
Configuración y resultado de un run de evaluación.

Dos dataclasses tipadas, auto-serializables a JSON, que viajan juntas:
  - RunConfig describe QUÉ se va a probar (input).
  - RunResult describe QUÉ pasó (output) y se persiste en data/eval_runs/.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class RunConfig:
    """Describe la versión del sistema bajo evaluación.

    Cada combinación de campos define una "configuración" cuya métrica se
    incluirá en una fila de la tabla de ablación final del TFM.
    """
    name:        str                  # "baseline", "+context", "+rag", ...
    target_lang: str = "es"
    cpl_limit:   int = 42
    context:     str = ""             # contexto global a inyectar en el prompt
    chunk_size:  int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    """Salida completa de una ejecución, serializable a JSON.

    Incluye toda la metadata necesaria para que un revisor del TFM pueda
    reproducir la ejecución exactamente: hash del commit, configuración,
    timestamp, predicciones generadas y métricas calculadas.
    """
    config:        RunConfig
    timestamp:     str                       # ISO 8601
    git_commit:    str                       # hash corto, p.ej. "5f8743e"
    n_pairs:       int                       # cuántos pares del test-set se evaluaron
    elapsed_s:     float                     # duración total del run
    tokens_prompt: int = 0
    tokens_completion: int = 0
    metrics:       dict[str, Any] = field(default_factory=dict)
    predictions:   list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config":            self.config.to_dict(),
            "timestamp":         self.timestamp,
            "git_commit":        self.git_commit,
            "n_pairs":           self.n_pairs,
            "elapsed_s":         self.elapsed_s,
            "tokens_prompt":     self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "metrics":           self.metrics,
            "predictions":       self.predictions,
        }


def now_iso() -> str:
    """Timestamp ISO 8601 con segundos, sin microsegundos."""
    return datetime.now().isoformat(timespec="seconds")
