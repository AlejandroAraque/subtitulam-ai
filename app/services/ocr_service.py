"""
Servicio OCR — detección y lectura de texto en frames de vídeo para Idea 2
(v3.2).

Nivel 1: solo detección — devuelve timestamps de frames con texto en
pantalla, junto con bounding boxes y un thumbnail JPEG para mostrar en UI.
NO lee el contenido del texto todavía (eso es Nivel 2: usar reader.readtext
en lugar de reader.detect).

Pipeline:
  1. `extract_frames(path, interval_s)` — sample del .mp4 cada N segundos
     con OpenCV. Devuelve lista de (timestamp_s, frame_bgr).
  2. `detect_text_in_frames(frames, progress_cb)` — para cada frame llama
     a EasyOCR.detect() (solo bounding boxes, mucho más rápido que OCR
     completo). Filtra los que no tienen texto y genera thumbnail.

Implementación con EasyOCR + OpenCV. Idiomas inglés y español por defecto.
CPU-only (sin requisito de GPU); ~0.5 s por frame en CPU moderna.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import cv2
import easyocr
import numpy as np


logger = logging.getLogger("subtitulam.ocr")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s · %(levelname)s · %(message)s", "%H:%M:%S"
    ))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# ── Singleton del Reader (carga modelos pesados solo una vez) ───────────────
# Primera ejecución descarga ~150 MB de modelos a ~/.EasyOCR/. Siguientes
# arranques son instantáneos (modelos cacheados).
_reader: Optional[easyocr.Reader] = None


def _get_reader() -> easyocr.Reader:
    """Devuelve el Reader EasyOCR, instanciándolo lazy la primera vez."""
    global _reader
    if _reader is None:
        logger.info("EasyOCR · cargando modelos (en, es) en CPU…")
        _reader = easyocr.Reader(
            ["en", "es"],
            gpu=False,
            verbose=False,
        )
        logger.info("EasyOCR · modelos cargados")
    return _reader


# ── Extracción de frames ────────────────────────────────────────────────────

def extract_frames(
    video_path: str,
    interval_s: float = 2.0,
) -> list[tuple[float, np.ndarray]]:
    """Extrae frames del vídeo cada `interval_s` segundos.

    Args:
        video_path: ruta absoluta o relativa al archivo de vídeo.
        interval_s: intervalo de sampling en segundos. Menor = más cubrimiento
            pero más coste; valor típico 1-3 s.

    Returns:
        Lista de tuplas (timestamp_s, frame_bgr_ndarray).

    Raises:
        IOError si el vídeo no se puede abrir.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"No se pudo abrir el vídeo: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_s = total_frames / fps if fps > 0 else 0.0

    frames: list[tuple[float, np.ndarray]] = []
    t = 0.0
    while t <= duration_s:
        # POS_MSEC: pide al decoder ir a ese tiempo en ms; OpenCV redondea
        # al keyframe más cercano, lo que puede dar pequeños desajustes
        # — aceptable para sampling de detección.
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if ok and frame is not None:
            frames.append((t, frame))
        t += interval_s
    cap.release()

    logger.info(
        "extract_frames · video=%s dur=%.1fs interval=%.1fs -> %d frames",
        Path(video_path).name, duration_s, interval_s, len(frames),
    )
    return frames


# ── Thumbnails para la UI ───────────────────────────────────────────────────

def _frame_to_thumbnail(frame_bgr: np.ndarray, target_width: int = 240) -> bytes:
    """Reduce el frame a `target_width` px de ancho preservando aspect ratio
    y devuelve un JPEG en bytes (calidad 80, balance tamaño/calidad).
    """
    h, w = frame_bgr.shape[:2]
    scale = target_width / w if w > 0 else 1.0
    target_h = max(1, int(round(h * scale)))
    thumb = cv2.resize(
        frame_bgr, (target_width, target_h),
        interpolation=cv2.INTER_AREA,
    )
    ok, jpeg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise IOError("cv2.imencode falló al generar el thumbnail")
    return jpeg.tobytes()


def _draw_bboxes_on_thumbnail(
    frame_bgr: np.ndarray,
    bboxes: list[list[int]],
    target_width: int = 240,
) -> bytes:
    """Dibuja las bounding boxes sobre el frame antes de generar el
    thumbnail. Útil para que el usuario vea visualmente DÓNDE se detectó
    el texto en cada frame.
    """
    h, w = frame_bgr.shape[:2]
    scale = target_width / w if w > 0 else 1.0
    target_h = max(1, int(round(h * scale)))

    # Dibujamos antes del resize para que las líneas sean nítidas.
    overlay = frame_bgr.copy()
    for bbox in bboxes:
        # EasyOCR devuelve [x_min, x_max, y_min, y_max] para horizontal_list.
        x_min, x_max, y_min, y_max = bbox
        cv2.rectangle(
            overlay,
            (int(x_min), int(y_min)),
            (int(x_max), int(y_max)),
            color=(0, 200, 255),  # ámbar BGR
            thickness=max(2, int(min(w, h) * 0.004)),
        )

    thumb = cv2.resize(overlay, (target_width, target_h),
                       interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise IOError("cv2.imencode falló al generar el thumbnail")
    return jpeg.tobytes()


# ── Detección (Nivel 1: solo bboxes, sin OCR) ───────────────────────────────

def detect_text_in_frames(
    frames: list[tuple[float, np.ndarray]],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Para cada frame detecta regiones con texto. Solo bboxes, sin leer
    el contenido (Nivel 2 lo leerá llamando a reader.readtext).

    Args:
        frames: salida de `extract_frames`.
        progress_callback: opcional, función `(done, total) -> None` para
            actualizar una progress bar en la UI.

    Returns:
        Lista de dicts con keys:
          timestamp_s : float
          n_regions   : int (número de regiones de texto detectadas)
          bboxes      : list[list[int]] (cada bbox = [x_min, x_max, y_min, y_max])
          thumbnail   : bytes (JPEG con las bboxes dibujadas para QA visual)

        SOLO incluye frames donde se detectó al menos una región.
    """
    reader = _get_reader()
    results: list[dict] = []
    total = len(frames)

    for i, (ts, frame) in enumerate(frames):
        if progress_callback is not None:
            progress_callback(i + 1, total)
        try:
            # reader.detect() devuelve (horizontal_list, free_list).
            # horizontal_list = bboxes axis-aligned [x_min, x_max, y_min, y_max].
            detection = reader.detect(frame)
            h_boxes = detection[0][0] if detection and len(detection) > 0 else []
        except Exception as e:
            logger.warning("detect frame %.2fs falló: %s", ts, e)
            continue

        if not h_boxes:
            continue

        bboxes_int = [list(map(int, bbox)) for bbox in h_boxes]
        results.append({
            "timestamp_s": ts,
            "n_regions":   len(h_boxes),
            "bboxes":      bboxes_int,
            "thumbnail":   _draw_bboxes_on_thumbnail(frame, bboxes_int),
        })

    logger.info(
        "detect_text_in_frames · %d/%d frames con texto detectado",
        len(results), total,
    )
    return results
