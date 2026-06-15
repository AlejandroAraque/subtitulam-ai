"""Buffer de logs por job_uuid en memoria, thread-safe.

Pensado para que el frontend pueda hacer polling al backend y mostrar
progreso real durante una traducción, sin tocar el logger de Python ni
inflar la base de datos.

Flujo:
    1. El frontend genera un uuid por job y lo manda al backend en el
       form-data de POST /translate (`job_uuid`).
    2. El backend llama a `log(uuid, "mensaje")` en los hitos relevantes
       (carga glosario, RAG, cada chunk enviado/recibido).
    3. El frontend hace polling a GET /jobs/by-uuid/{uuid}/logs?since=N
       cada ~2s y muestra las líneas nuevas.

Diseño:
    - Singleton: dict global protegido por un lock global. Suficiente
      para single-user / pocos jobs concurrentes (que es nuestro caso:
      el worker del frontend procesa uno a uno).
    - GC pasivo: en cada `log()` se descartan buffers con última
      escritura > MAX_BUFFER_AGE_S, sin thread aparte.
    - Sin persistencia: si reinicias el backend, los logs históricos se
      pierden. Los del job activo también, pero solo durante una
      ventana ≤2s (el siguiente poll del frontend lo refleja como vacío
      y el job termina por timeout normal).

Si en el futuro necesitamos multi-user o restart-resilient, se reemplaza
el dict por Redis sin cambiar la API pública.
"""

from __future__ import annotations

import threading
import time
from typing import TypedDict

# ── Configuración ───────────────────────────────────────────────────────────
MAX_LINES_PER_JOB = 500        # tras eso, se descarta la línea más antigua
MAX_BUFFER_AGE_S  = 3600.0     # buffers sin actividad >1h se purgan


class LogEntry(TypedDict):
    seq:       int      # offset incremental, 0-based, para polling con `since`
    ts:        float    # unix timestamp
    level:     str      # "info" | "warn" | "error"
    message:   str


class _JobBuffer(TypedDict):
    lines:    list[LogEntry]
    seq:      int          # próximo seq a asignar
    last_ts:  float        # para GC


_lock: threading.Lock = threading.Lock()
_buffers: dict[str, _JobBuffer] = {}


def _gc_locked(now: float) -> None:
    """Purga buffers con last_ts antiguo. Llamar con _lock adquirido."""
    expired = [uid for uid, buf in _buffers.items()
               if now - buf["last_ts"] > MAX_BUFFER_AGE_S]
    for uid in expired:
        del _buffers[uid]


def log(job_uuid: str, message: str, level: str = "info") -> None:
    """Añade una línea al buffer del job. No-op si `job_uuid` es vacío."""
    if not job_uuid:
        return
    now = time.time()
    with _lock:
        _gc_locked(now)
        buf = _buffers.get(job_uuid)
        if buf is None:
            buf = {"lines": [], "seq": 0, "last_ts": now}
            _buffers[job_uuid] = buf
        entry: LogEntry = {
            "seq":     buf["seq"],
            "ts":      now,
            "level":   level,
            "message": message,
        }
        buf["lines"].append(entry)
        buf["seq"] += 1
        buf["last_ts"] = now
        # Cap del tamaño para que un job patológico no consuma RAM sin freno
        if len(buf["lines"]) > MAX_LINES_PER_JOB:
            buf["lines"] = buf["lines"][-MAX_LINES_PER_JOB:]


def get(job_uuid: str, since: int = 0) -> list[LogEntry]:
    """Devuelve las líneas con seq >= since. Lista vacía si no hay buffer."""
    if not job_uuid:
        return []
    with _lock:
        buf = _buffers.get(job_uuid)
        if buf is None:
            return []
        return [e for e in buf["lines"] if e["seq"] >= since]


def clear(job_uuid: str) -> None:
    """Elimina el buffer (úsese al cerrar un job con éxito o error)."""
    if not job_uuid:
        return
    with _lock:
        _buffers.pop(job_uuid, None)
