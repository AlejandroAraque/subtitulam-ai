"""
Servicio OCR — detección, lectura y traducción de texto en frames de vídeo
para Idea 2 (v3.2).

Tres niveles de funcionalidad incrementales:
  Nivel 1 (`detect_text_in_frames`): solo bboxes, rápido.
  Nivel 2 (`read_text_in_frames`): bboxes + texto leído + confianza.
  Nivel 3 (`translate_detections`): traduce los textos leídos con gpt-4o-mini.

Pipeline:
  1. `extract_frames(path, interval_s)` — sample del .mp4 cada N segundos
     con OpenCV. Devuelve lista de (timestamp_s, frame_bgr).
  2. `detect_text_in_frames` o `read_text_in_frames` — pasa cada frame por
     EasyOCR. Filtra falsos positivos de subtítulos quemados (15% inferior
     del frame). Genera thumbnail con bboxes ámbar dibujadas.
  3. `translate_detections(detections, target_lang)` — pre-traduce todos
     los textos leídos en una sola llamada batch a gpt-4o-mini.

Implementación con EasyOCR + OpenCV + OpenAI. Idiomas inglés y español
por defecto. CPU-only (sin requisito de GPU); ~6-7 s por frame a 1080p
con detección, ~9-10 s con lectura completa.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable, Optional

import cv2
import easyocr
import numpy as np
from openai import AsyncOpenAI


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
    max_detect_width: int = 1280,
) -> list[dict]:
    """Para cada frame detecta regiones con texto. Solo bboxes, sin leer
    el contenido (Nivel 2 lo leerá llamando a reader.readtext).

    Optimización: si el frame original es más ancho que `max_detect_width`
    (típicamente 1080p o superior), se redimensiona antes de pasar a EasyOCR.
    Reduce el coste ~2-3× con pérdida de precisión despreciable. Las
    bboxes se reescalan al tamaño original para que coincidan con el
    thumbnail.

    Args:
        frames: salida de `extract_frames`.
        progress_callback: opcional, función `(done, total) -> None` para
            actualizar una progress bar en la UI.
        max_detect_width: ancho máximo del frame a pasar al detector.
            Default 1280 (720p). Subir para más precisión, bajar para más
            velocidad.

    Returns:
        Lista de dicts con keys:
          timestamp_s : float
          n_regions   : int (número de regiones de texto detectadas)
          bboxes      : list[list[int]] (cada bbox = [x_min, x_max, y_min, y_max]
                                        en coordenadas del frame ORIGINAL)
          thumbnail   : bytes (JPEG con las bboxes dibujadas para QA visual)

        SOLO incluye frames donde se detectó al menos una región.
    """
    reader = _get_reader()
    results: list[dict] = []
    total = len(frames)

    for i, (ts, frame) in enumerate(frames):
        if progress_callback is not None:
            progress_callback(i + 1, total)

        # Redimensionar a max_detect_width si es necesario (acelera ~2-3×)
        h, w = frame.shape[:2]
        if w > max_detect_width:
            scale = max_detect_width / w
            scaled = cv2.resize(
                frame,
                (max_detect_width, int(round(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            scale = 1.0
            scaled = frame

        try:
            # reader.detect() devuelve (horizontal_list, free_list).
            # horizontal_list = bboxes axis-aligned [x_min, x_max, y_min, y_max].
            detection = reader.detect(scaled)
            h_boxes_scaled = detection[0][0] if detection and len(detection) > 0 else []
        except Exception as e:
            logger.warning("detect frame %.2fs falló: %s", ts, e)
            continue

        if not h_boxes_scaled:
            continue

        # Reescalar bboxes al tamaño ORIGINAL del frame
        if scale != 1.0:
            inv = 1.0 / scale
            bboxes_int = [
                [int(round(bbox[0] * inv)), int(round(bbox[1] * inv)),
                 int(round(bbox[2] * inv)), int(round(bbox[3] * inv))]
                for bbox in h_boxes_scaled
            ]
        else:
            bboxes_int = [list(map(int, bbox)) for bbox in h_boxes_scaled]

        results.append({
            "timestamp_s": ts,
            "n_regions":   len(h_boxes_scaled),
            "bboxes":      bboxes_int,
            "thumbnail":   _draw_bboxes_on_thumbnail(frame, bboxes_int),
        })

    logger.info(
        "detect_text_in_frames · %d/%d frames con texto detectado",
        len(results), total,
    )
    return results


# ── Lectura (Nivel 2: bboxes + texto + confianza) ────────────────────────

def _is_in_subtitle_zone(bbox: list[int], frame_h: int, threshold: float = 0.80) -> bool:
    """Devuelve True si el bbox está en la zona inferior del frame
    (típicamente donde van los subtítulos quemados).

    `threshold=0.80` significa que cualquier bbox cuyo y_min esté por
    debajo del 80% de la altura se considera subtítulo. Filtrar estos
    evita falsos positivos cuando el .srt está quemado en el vídeo.
    """
    _x_min, _x_max, y_min, _y_max = bbox
    return y_min >= frame_h * threshold


def read_text_in_frames(
    frames: list[tuple[float, np.ndarray]],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_detect_width: int = 1280,
    min_confidence: float = 0.4,
    filter_subtitle_zone: bool = True,
) -> list[dict]:
    """Para cada frame detecta Y LEE el texto con EasyOCR.

    Más lento que `detect_text_in_frames` (~30-50% extra por frame)
    pero devuelve el contenido de cada región, no solo dónde está.

    Args:
        frames:               salida de `extract_frames`.
        progress_callback:    función `(done, total) -> None` opcional.
        max_detect_width:     ancho máximo del frame al pasar a OCR.
        min_confidence:       descarta regiones con confianza menor
                              (0.0..1.0). Default 0.4.
        filter_subtitle_zone: si True, descarta bboxes en el 20% inferior
                              del frame (zona típica de subtítulos quemados).

    Returns:
        Lista de dicts con:
          timestamp_s : float
          text        : str (texto leído, concatenando regiones con " · ")
          confidence  : float (confianza promedio del frame)
          bboxes      : list[list[int]] (coords del frame ORIGINAL)
          regions     : list[dict] (cada región: text, confidence, bbox)
          thumbnail   : bytes (JPEG con bboxes dibujadas)

        Solo frames con al menos 1 región tras filtrado.
    """
    reader = _get_reader()
    results: list[dict] = []
    total = len(frames)

    for i, (ts, frame) in enumerate(frames):
        if progress_callback is not None:
            progress_callback(i + 1, total)

        h, w = frame.shape[:2]
        # Resize antes del OCR (acelera ~2-3×)
        if w > max_detect_width:
            scale = max_detect_width / w
            scaled = cv2.resize(
                frame,
                (max_detect_width, int(round(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            scale = 1.0
            scaled = frame

        try:
            # readtext devuelve [(bbox_4_puntos, text, confidence), ...]
            raw = reader.readtext(scaled)
        except Exception as e:
            logger.warning("readtext frame %.2fs falló: %s", ts, e)
            continue

        if not raw:
            continue

        # Construir regiones filtrando por confianza y zona de subtítulo
        inv = 1.0 / scale
        regions = []
        bboxes_int = []
        for entry in raw:
            bbox_4pts, text, conf = entry
            if conf < min_confidence:
                continue
            # bbox_4pts = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]] aprox
            xs = [p[0] for p in bbox_4pts]
            ys = [p[1] for p in bbox_4pts]
            bbox_axis = [
                int(round(min(xs) * inv)),
                int(round(max(xs) * inv)),
                int(round(min(ys) * inv)),
                int(round(max(ys) * inv)),
            ]
            if filter_subtitle_zone and _is_in_subtitle_zone(bbox_axis, h):
                continue
            regions.append({
                "text":       text.strip(),
                "confidence": round(float(conf), 3),
                "bbox":       bbox_axis,
            })
            bboxes_int.append(bbox_axis)

        if not regions:
            continue

        avg_conf = round(
            sum(r["confidence"] for r in regions) / len(regions), 3,
        )
        combined_text = " · ".join(r["text"] for r in regions)

        results.append({
            "timestamp_s": ts,
            "text":        combined_text,
            "confidence":  avg_conf,
            "bboxes":      bboxes_int,
            "regions":     regions,
            "thumbnail":   _draw_bboxes_on_thumbnail(frame, bboxes_int),
        })

    logger.info(
        "read_text_in_frames · %d/%d frames con texto legible "
        "(min_conf=%.2f, filter_subs=%s)",
        len(results), total, min_confidence, filter_subtitle_zone,
    )
    return results


# ── Traducción de los textos detectados (Nivel 3) ────────────────────────

_translate_client: Optional[AsyncOpenAI] = None


def _get_translate_client() -> AsyncOpenAI:
    """Cliente OpenAI singleton para traducción de textos OCR.
    Separado del cliente principal de `translation_service` por claridad."""
    global _translate_client
    if _translate_client is None:
        _translate_client = AsyncOpenAI()
    return _translate_client


async def translate_detections(
    detections: list[dict],
    target_lang: str = "es",
    model: str = "gpt-4o-mini",
) -> list[dict]:
    """Traduce el campo `text` de cada detección con gpt-4o-mini en una
    sola llamada batch al LLM.

    El input al modelo es una lista numerada de textos en inglés; la
    respuesta esperada es la misma lista en español. Coste estimado:
    ~$0.0001 por 10 textos (negligible).

    Args:
        detections: salida de `read_text_in_frames`.
        target_lang: "es" / "es-419" / etc.
        model: por defecto gpt-4o-mini (60× más barato que gpt-4o).

    Returns:
        Las mismas detecciones con un campo nuevo `text_translated`.
    """
    if not detections:
        return detections

    lang_label = {
        "es":     "español de España",
        "es-419": "español neutro de Latinoamérica",
        "fr":     "francés",
        "de":     "alemán",
        "pt":     "portugués de Brasil",
        "it":     "italiano",
    }.get(target_lang, "español de España")

    # Numeramos para poder mapear respuesta → detección
    numbered = "\n".join(
        f"{i+1}: {d['text']}" for i, d in enumerate(detections)
    )

    system_prompt = (
        f"Eres un traductor profesional. Traduce al {lang_label} los "
        "textos en pantalla siguientes (letreros, carteles, mensajes). "
        "Mantén el registro y la concisión. Los nombres propios no se "
        "traducen. Si el texto es ambiguo o muy corto (1-2 letras), "
        "devuélvelo tal cual.\n\n"
        "FORMATO DE SALIDA: una traducción por línea, prefijada con "
        '"N:" exactamente con el mismo número que recibiste. Sin '
        "comentarios extra."
    )

    client = _get_translate_client()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": numbered},
            ],
            temperature=0.2,
            max_tokens=2000,
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        logger.warning("translate_detections falló: %s", e)
        # Devolver detecciones sin traducción en caso de error
        for d in detections:
            d.setdefault("text_translated", "")
        return detections

    # Parsear "N: traducción" línea por línea
    translations: dict[int, str] = {}
    line_re = re.compile(r"^\s*(\d+)\s*[:.\-]\s*(.+?)\s*$")
    for line in raw.splitlines():
        m = line_re.match(line)
        if m:
            translations[int(m.group(1))] = m.group(2).strip()

    # Asignar traducciones a las detecciones (1-indexed → 0-indexed)
    for i, d in enumerate(detections):
        d["text_translated"] = translations.get(i + 1, "")

    logger.info(
        "translate_detections · %d/%d textos traducidos con %s",
        sum(1 for d in detections if d["text_translated"]),
        len(detections), model,
    )
    return detections
