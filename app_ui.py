"""
Subtitulam — Streamlit app (high-fidelity).
Replica el prototipo React en un único archivo usando CSS inyectado,
componentes HTML personalizados y st.session_state para el flujo SPA.

Ejecutar:  streamlit run app_ui.py
Backend:   uvicorn app.main:app --reload   (puerto 8000)
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from html import escape
from typing import Optional

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

# Cargar .env (mismo .env que usa el backend). Sin esto, `uv run streamlit`
# no inyecta las variables y la traducción OCR (gpt-4o-mini) falla por
# OPENAI_API_KEY ausente.
load_dotenv()

# Configurable vía env para que docker-compose pueda apuntar al servicio
# "backend" en lugar de localhost. Default localhost para dev local sin Docker.
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# ── Normas de subtitulado ────────────────────────────────────────────────────
# UNE 153010 (TV España): 38 caracteres por línea. Netflix: 42. El default de
# la app es la norma UNE porque es la que usan los estudios españoles; cada
# encargo puede cambiarla desde el slider del Workspace.
CPL_UNE = 38
CPL_NETFLIX = 42
DEFAULT_CPL = CPL_UNE
CPS_LIMIT = 17.0

# ── Tarifas OpenAI para la columna de coste ─────────────────────────────────
# El coste NO es una estimación inventada: OpenAI reporta los tokens exactos
# de cada request (usage.prompt_tokens / completion_tokens), la app los suma
# y persiste por trabajo, y la factura de OpenAI es exactamente
# tokens × tarifa. Estas constantes son la tarifa oficial de gpt-4o
# (openai.com/api/pricing, vigente 2026-07: $2.50/M entrada · $10/M salida).
# Si OpenAI cambia precios, actualizar aquí o vía variables de entorno.
# Verificable: la suma de la columna debe cuadrar con platform.openai.com/usage.
GPT4O_USD_PER_M_INPUT = float(os.getenv("GPT4O_USD_PER_M_INPUT", "2.50"))
GPT4O_USD_PER_M_OUTPUT = float(os.getenv("GPT4O_USD_PER_M_OUTPUT", "10.00"))
EUR_PER_USD = float(os.getenv("EUR_PER_USD", "0.92"))  # cambio aprox., ajustable


def _job_cost_eur(tokens_prompt: int | None, tokens_completion: int | None) -> float | None:
    """Coste en EUR de un job a partir de sus tokens persistidos."""
    if not tokens_prompt and not tokens_completion:
        return None
    usd = ((tokens_prompt or 0) * GPT4O_USD_PER_M_INPUT
           + (tokens_completion or 0) * GPT4O_USD_PER_M_OUTPUT) / 1_000_000
    return usd * EUR_PER_USD


# ═══════════════════════════════════════════════════════════════════════════
# API HELPERS — capa fina sobre requests para hablar con el backend.
# Todas devuelven valores "seguros" cuando el backend no responde, evitando
# que la UI explote si uvicorn está caído.
# ═══════════════════════════════════════════════════════════════════════════
@st.cache_data(show_spinner=False)
def api_get_glossary() -> list[dict]:
    """Lista los términos del glosario. [] si el backend no responde.

    Cacheada sin TTL: dura toda la sesión hasta que se invalida con
    api_get_glossary.clear() tras una mutación (POST/DELETE). Esto da
    latencia predictible: ~1s la primera visita, instantáneo en las
    siguientes mientras no haya cambios.
    """
    try:
        r = requests.get(f"{BACKEND_URL}/glossary", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return []


def api_post_glossary(source: str, target: str, category: str, note: str) -> Optional[dict]:
    """Crea un término. Devuelve el dict creado o None si falla."""
    try:
        r = requests.post(
            f"{BACKEND_URL}/glossary",
            json={"source": source, "target": target, "category": category, "note": note},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        st.toast(f"No se pudo guardar el término: {e}", icon="⚠")
        return None


def api_delete_glossary(term_id: int) -> bool:
    """Borra un término por id. True si se borró, False si no existía o falló."""
    try:
        r = requests.delete(f"{BACKEND_URL}/glossary/{term_id}", timeout=10)
        return r.status_code == 204
    except requests.exceptions.RequestException:
        return False


@st.cache_data(show_spinner=False, ttl=15)
def api_get_jobs() -> list[dict]:
    """Lista los jobs (historial) más recientes primero.

    ttl=15: con la cola en el backend (v3.8), los jobs cambian de estado
    desde OTRO proceso — la invalidación manual de esta sesión ya no
    basta. El coste es predecible: un GET local (~ms, sin N+1) como
    mucho cada 15 s y solo al visitar el Historial.
    """
    try:
        r = requests.get(f"{BACKEND_URL}/jobs", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return []


def api_delete_job(job_id: int) -> bool:
    """Borra un job y, en cascada, sus translations."""
    try:
        r = requests.delete(f"{BACKEND_URL}/jobs/{job_id}", timeout=10)
        return r.status_code == 204
    except requests.exceptions.RequestException:
        return False


@st.cache_data(show_spinner=False)
def _fetch_job_srt_cached(job_id: int) -> bytes:
    """SRT archivado de un job completado. Cacheado: es inmutable.
    Lanza en fallo — las excepciones NO se cachean, así un backend caído
    de forma transitoria no envenena la cache de todas las sesiones."""
    r = requests.get(f"{BACKEND_URL}/jobs/{job_id}/download", timeout=10)
    r.raise_for_status()
    return r.content


def _fetch_job_srt(job_id: int) -> bytes | None:
    """Wrapper tolerante: None si no disponible (job antiguo sin archivo
    o backend caído), sin cachear el fallo."""
    try:
        return _fetch_job_srt_cached(job_id)
    except Exception:
        return None


def _parse_srt_bytes(srt_bytes: bytes) -> list[dict]:
    """Parser SRT mínimo: devuelve lista de {index, start_s, end_s, text}.

    Acepta bytes UTF-8 (con o sin BOM) y ambos separadores horarios
    (',' y '.'). No valida formato estricto; cues malformados se ignoran.
    """
    import re as _re
    text = srt_bytes.decode("utf-8-sig", errors="replace")
    ts_re = _re.compile(
        r'(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*'
        r'(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})'
    )
    cues = []
    for blk in _re.split(r'\n\s*\n', text):
        lines = blk.strip().splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            continue
        m = ts_re.match(lines[1].strip())
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        start_s = h1*3600 + m1*60 + s1 + ms1/1000
        end_s   = h2*3600 + m2*60 + s2 + ms2/1000
        cues.append({
            "index":   int(lines[0]),
            "start_s": start_s,
            "end_s":   end_s,
            "text":    "\n".join(lines[2:]).strip(),
        })
    return cues


def _srt_to_vtt(srt_bytes: bytes) -> bytes:
    """Convierte SRT a WebVTT para el reproductor HTML5 de st.video.

    El navegador solo soporta WebVTT como pista de subtítulos. Streamlit
    intenta convertir SRT internamente, pero la conversión falla con
    silencios (BOM, line endings mixtos, encoding sospechoso) y entonces
    el botón CC ni siquiera aparece en el reproductor. Hacerlo nosotros
    explícitamente garantiza una pista válida.

    Diferencias SRT → VTT:
      - Cabecera "WEBVTT" obligatoria al principio.
      - Los milisegundos van con punto, no con coma:
            00:00:01,500 (SRT)  ->  00:00:01.500 (VTT)
      - Normalizamos line endings a \\n.
    """
    import re as _re
    text = srt_bytes.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return ("WEBVTT\n\n" + text).encode("utf-8")


def _format_timestamp(seconds: float) -> str:
    """Convierte segundos a 'HH:MM:SS' para mostrar en la UI."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_srt_timestamp(seconds: float) -> str:
    """Convierte segundos a 'HH:MM:SS,mmm' (formato SRT estándar)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _compute_cue_metrics(
    cue: dict,
    next_cue: dict | None = None,
    cpl_limit: int = DEFAULT_CPL,
    cps_limit: float = CPS_LIMIT,
    min_duration_s: float = 0.500,
    max_duration_s: float = 7.0,
    min_gap_s: float = 0.080,
) -> dict:
    """Métricas técnicas del cue según convenciones de subtitulado profesional
    (UNE 153010 + Netflix style guide).

    Returns:
        dict con:
          status   : 'ok' | 'warn' | 'err' (peor de las métricas)
          cpl_max  : int  — longitud de la línea más larga
          cps      : float — caracteres por segundo
          duration : float — duración en segundos
          n_lines  : int  — líneas del cue
          gap_s    : float | None — gap con el cue siguiente (None si es último)
          issues   : list[str] — descripciones legibles para tooltip
    """
    text = cue["text"]
    lines = [ln for ln in text.split("\n") if ln.strip()]
    cpl_max = max((len(ln) for ln in lines), default=0)
    chars = sum(len(ln) for ln in lines)
    duration = max(cue["end_s"] - cue["start_s"], 0.001)
    cps = chars / duration

    issues: list[str] = []
    rank = {"ok": 0, "warn": 1, "err": 2}
    worst = "ok"

    def _bump(level: str) -> None:
        nonlocal worst
        if rank[level] > rank[worst]:
            worst = level

    if cpl_max > cpl_limit:
        issues.append(f"CPL {cpl_max} > {cpl_limit}")
        _bump("warn")
    if cps > cps_limit:
        issues.append(f"CPS {cps:.1f} > {cps_limit}")
        _bump("warn")
    if duration < min_duration_s:
        issues.append(f"Dur {duration:.2f}s < {min_duration_s}s")
        _bump("err")
    elif duration > max_duration_s:
        issues.append(f"Dur {duration:.1f}s > {max_duration_s}s")
        _bump("warn")
    if len(lines) > 2:
        issues.append(f"{len(lines)} líneas (máx 2)")
        _bump("warn")

    gap_s = None
    if next_cue is not None:
        gap_s = next_cue["start_s"] - cue["end_s"]
        if gap_s < 0:
            issues.append(f"Solapa {abs(gap_s)*1000:.0f}ms con cue siguiente")
            _bump("err")
        elif gap_s < min_gap_s:
            issues.append(f"Gap {gap_s*1000:.0f}ms < {min_gap_s*1000:.0f}ms")
            _bump("warn")

    return {
        "status":   worst,
        "cpl_max":  cpl_max,
        "cps":      round(cps, 1),
        "duration": round(duration, 2),
        "n_lines":  len(lines),
        "gap_s":    round(gap_s, 3) if gap_s is not None else None,
        "issues":   issues,
    }


def _build_modified_srt(
    cues: list[dict],
    edits: dict[int, str],
    deleted: set[int] | None = None,
    added: list[dict] | None = None,
) -> bytes:
    """Reconstruye un .srt aplicando los cambios del usuario sobre los
    cues originales.

    Args:
        cues:    lista de cues parseados del .srt original.
        edits:   {cue_idx → nuevo_texto} para cues editados.
        deleted: {cue_idx} para cues omitidos.
        added:   lista de cues añadidos manualmente, cada uno con
                 {id, start_s, end_s, text}.

    Returns:
        bytes UTF-8 del .srt resultante, con índices renumerados
        consecutivos y los cues (originales + añadidos) ordenados por
        timestamp de inicio.
    """
    deleted = deleted or set()
    added   = added or []

    # Unificar: originales (no eliminados, con edits aplicados) + añadidos.
    merged: list[dict] = []
    for c in cues:
        if c["index"] in deleted:
            continue
        merged.append({
            "start_s": c["start_s"],
            "end_s":   c["end_s"],
            "text":    edits.get(c["index"], c["text"]),
        })
    for a in added:
        merged.append({
            "start_s": a["start_s"],
            "end_s":   a["end_s"],
            "text":    a["text"],
        })

    # Orden cronológico por start_s.
    merged.sort(key=lambda c: c["start_s"])

    parts: list[str] = []
    for i, c in enumerate(merged, start=1):
        parts.append(str(i))
        parts.append(
            f'{_format_srt_timestamp(c["start_s"])} --> '
            f'{_format_srt_timestamp(c["end_s"])}'
        )
        parts.append(c["text"])
        parts.append("")
    return "\n".join(parts).encode("utf-8")


def _build_glossary_csv(glossary: list[dict]) -> bytes:
    """Construye el CSV (BOM UTF-8 + ; separator) localmente desde los
    términos ya cargados en memoria. Evita una segunda llamada HTTP al
    backend en cada render — la sección CSV ya no bloquea el primer paint
    de la página.
    """
    import csv as _csv
    import io as _io
    output = _io.StringIO()
    writer = _csv.DictWriter(
        output,
        fieldnames=["source", "target", "category", "note"],
        delimiter=";",
        quoting=_csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for t in glossary:
        writer.writerow({
            "source":   t.get("source", ""),
            "target":   t.get("target", ""),
            "category": t.get("category", "término"),
            "note":     t.get("note", ""),
        })
    return ("﻿" + output.getvalue()).encode("utf-8")


def api_import_glossary_csv(filename: str, file_bytes: bytes) -> dict:
    """Sube un CSV al backend. Devuelve dict:
        - {'ok': True, 'imported': N, 'skipped': M, 'errors': [...]}
        - {'ok': False, 'detail': mensaje}
    No pinta nada en pantalla; eso es responsabilidad del caller.
    """
    try:
        r = requests.post(
            f"{BACKEND_URL}/glossary/import",
            files={"file": (filename, file_bytes, "text/csv")},
            timeout=30,
        )
        r.raise_for_status()
        return {"ok": True, **r.json()}
    except requests.exceptions.HTTPError as e:
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        return {"ok": False, "detail": detail}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "detail": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ═══════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Subtitulam",
    page_icon="✅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── SIDEBAR FIJA (no colapsable) ─────────────────────────────────────────
# Streamlit 1.56 usa estos data-testid. Forzamos visibilidad total y
# ocultamos los botones de colapso para que la barra lateral no pueda cerrarse.
st.markdown("""
<style>
/* Sidebar siempre visible, ancho fijo, sin transform de colapso */
[data-testid="stSidebar"],
section[data-testid="stSidebar"]{
  min-width:240px!important;
  max-width:240px!important;
  width:240px!important;
  transform:translateX(0)!important;
  visibility:visible!important;
  display:block!important;
  margin-left:0!important;
  left:0!important;
  position:relative!important;
}
/* Esconder todos los controles de colapso (cerrar + reabrir) */
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"],
button[kind="headerNoPadding"]{
  display:none!important;
}
/* Cabecera reservada vacía del sidebar (Streamlit 1.56) — quitar hueco */
[data-testid="stSidebarHeader"]{
  display:none!important;
  height:0!important;
  margin:0!important;
  padding:0!important;
}
/* El contenido principal no debe ocultarse detrás */
[data-testid="stAppViewContainer"] > .main{
  margin-left:0!important;
}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════════════
# Solo las claves con lectores reales: las ws_* del flujo síncrono
# antiguo (barra de progreso local, resultado en sesión) murieron con
# la migración de la cola al backend (v3.8).
_DEFAULTS: dict = {
    "page":            "workspace",
    "context_global":  "",
    "cpl_limit":       DEFAULT_CPL,
    "target_lang":     "es",
}
for _k, _v in _DEFAULTS.items():
    st.session_state.setdefault(_k, _v)

# ═══════════════════════════════════════════════════════════════════════════
# CSS — tokens + componentes (replicando app-styles.css)
# ═══════════════════════════════════════════════════════════════════════════
st.markdown("""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">

<style>
:root{
  --bg:#f8f9fa; --surface:#ffffff; --surface-2:#fdfdfd;
  --line:#e2e8f0; --line-2:#f1f5f9;
  --text:#0f172a; --text-2:#334155; --text-3:#64748b; --text-4:#94a3b8; --text-5:#cbd5e1;
  --primary:#0f172a; --primary-ink:#ffffff;
  --hover-bg:#f1f5f9;
  --shadow-1:0 1px 3px rgba(0,0,0,.06);
  --shadow-2:0 1px 4px rgba(0,0,0,.04);
  --shadow-3:0 1px 2px rgba(0,0,0,.03);
  --ring:rgba(15,23,42,.07);
  --ok-bg:#f0fdf4; --ok-br:#bbf7d0; --ok-fg:#15803d; --ok-fg-2:#166534;
  --err-bg:#fef2f2; --err-br:#fecaca; --err-fg:#dc2626; --err-fg-2:#b91c1c;
  --warn-bg:#fffbeb;--warn-br:#fde68a;--warn-fg:#b45309;--warn-fg-2:#92400e;
  --info-bg:#eff6ff;--info-br:#bfdbfe;--info-fg:#1d4ed8;--info-fg-2:#1e40af;
  --font-sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --font-mono:'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace;
}

/* ── base ─────────────────────────────── */
html,body,[class*="css"]{font-family:var(--font-sans)!important;-webkit-font-smoothing:antialiased;}
.stApp{background:var(--bg)!important;}
.mono{font-family:var(--font-mono);font-feature-settings:"calt" 0;}

/* ── ocultar chrome de Streamlit ───────── */
#MainMenu,footer,.stDeployButton,[data-testid="stToolbar"],[data-testid="stDecoration"]{display:none!important;}
header{background:transparent!important;}
::-webkit-scrollbar{width:6px;}
::-webkit-scrollbar-track{background:var(--line-2);}
::-webkit-scrollbar-thumb{background:var(--text-5);border-radius:99px;}

/* ── contenedor principal ──────────────── */
.block-container{padding:32px 40px 56px!important;max-width:1100px!important;}

/* ── SIDEBAR ───────────────────────────── */
[data-testid="stSidebar"],section[data-testid="stSidebar"]{
  background:var(--surface)!important;
  border-right:1px solid var(--line)!important;
}
[data-testid="stSidebar"]>div:first-child,
[data-testid="stSidebarContent"]{
  padding:0!important;
  background:var(--surface)!important;
  height:100vh!important;
}
/* el contenedor de usuario es flex-column a pantalla completa
   para poder empujar el .sb-foot al fondo con margin-top:auto */
[data-testid="stSidebarUserContent"]{
  display:flex!important;
  flex-direction:column!important;
  min-height:calc(100vh - 32px)!important;
  padding:24px 16px 22px!important;
  background:var(--surface)!important;
}
/* Los botones de colapso están ocultos al inicio del archivo (sidebar fija) */

/* brand */
.sb-brand{
  display:flex;gap:12px;align-items:center;
  padding:4px 4px 22px;
  border-bottom:1px solid var(--line-2);
  margin:4px 0 18px;
}
.sb-logo{
  width:40px;height:40px;border-radius:11px;
  background:var(--primary);color:var(--primary-ink);
  display:flex;align-items:center;justify-content:center;
  font-size:18px;font-weight:700;letter-spacing:-.5px;
  flex-shrink:0;box-shadow:var(--shadow-1);
}
.sb-text{min-width:0;}
.sb-name{
  font-size:25px;font-weight:700;letter-spacing:-.4px;
  color:var(--text);line-height:1.2;
}
.sb-tag{
  font-size:12px;color:var(--text-4);margin-top:4px;
  text-transform:uppercase;letter-spacing:.1em;
  font-weight:600;white-space:nowrap;
}

/* nav items — impedir word-wrap feo */
[data-testid="stSidebar"] .stRadio>div>label{white-space:nowrap!important;}

/* radio nav */
[data-testid="stSidebar"] .stRadio>label{display:none!important;}
[data-testid="stSidebar"] .stRadio>div{gap:2px!important;flex-direction:column!important;}
[data-testid="stSidebar"] .stRadio>div>label{
  background:transparent!important;border-radius:7px!important;
  padding:8px 10px!important;cursor:pointer!important;width:100%!important;
  display:flex!important;align-items:center!important;font-size:13.2px!important;
  color:var(--text-3)!important;font-weight:400!important;
  transition:background .12s,color .12s!important;margin:0!important;user-select:none!important;
}
[data-testid="stSidebar"] .stRadio>div>label:hover{background:var(--hover-bg)!important;color:var(--text)!important;}
[data-testid="stSidebar"] .stRadio>div>label:has(input:checked){background:var(--hover-bg)!important;color:var(--text)!important;font-weight:500!important;}
[data-testid="stSidebar"] .stRadio input[type="radio"]{display:none!important;}
[data-testid="stSidebar"] .stRadio>div>label>div:first-child{display:none!important;}

/* Footer pegado al fondo del sidebar.
   Streamlit envuelve cada elemento en su propio contenedor, por lo que
   margin-top:auto NO funciona (el .sb-foot no es hijo directo del flex).
   Solución robusta: sacar el contenedor entero del flujo con position:absolute
   anclado al sidebar (que ya tiene position:relative). */
[data-testid="stSidebarUserContent"] [data-testid="stElementContainer"]:has(.sb-foot),
[data-testid="stSidebarUserContent"] div[data-testid="element-container"]:has(.sb-foot){
  position:absolute!important;
  bottom:22px!important;
  left:16px!important;
  right:16px!important;
  margin:0!important;
}
.sb-foot{
  padding:16px 6px 0;
  border-top:1px solid var(--line-2);
  font-size:11.5px;color:var(--text-4);line-height:1.65;
  background:var(--surface);
}
.sb-foot .dim{color:var(--text-5);margin-top:3px;}

/* ── PAGE HEADER ───────────────────────── */
.ph{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:24px;padding-bottom:18px;border-bottom:1px solid var(--line-2);gap:16px;}
.ph-t{margin:0;font-size:20px;font-weight:700;letter-spacing:-.5px;color:var(--text);line-height:1.25;}
.ph-s{margin:5px 0 0;font-size:13px;color:var(--text-4);font-weight:400;max-width:62ch;}
.ph-r{display:flex;gap:8px;align-items:center;}

/* section labels */
.sl{font-size:10.5px;font-weight:600;color:var(--text-4);text-transform:uppercase;letter-spacing:.08em;margin:0 0 10px;}
.sl-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}
.hint{font-size:11.5px;color:var(--text-4);margin-top:-8px;margin-bottom:18px;line-height:1.5;}

/* ── INPUTS & UPLOADER ─────────────────── */
.stTextInput>div>div>input,.stTextArea>div>div>textarea{
  background:var(--surface)!important;border:1px solid var(--line)!important;border-radius:8px!important;
  color:var(--text)!important;font-size:13.5px!important;padding:9px 12px!important;
  font-family:var(--font-sans)!important;box-shadow:var(--shadow-3)!important;transition:border-color .15s,box-shadow .15s;
}
.stTextInput>div>div>input:focus,.stTextArea>div>div>textarea:focus{
  border-color:var(--text)!important;box-shadow:0 0 0 3px var(--ring)!important;outline:none!important;
}
.stTextInput label,.stTextArea label,.stSelectbox label,.stSlider label,.stFileUploader label{
  font-size:12.5px!important;font-weight:500!important;color:var(--text-2)!important;
}
div[data-baseweb="select"]>div{
  background:var(--surface)!important;border:1px solid var(--line)!important;border-radius:8px!important;
  box-shadow:var(--shadow-3)!important;font-size:13.5px!important;
}

[data-testid="stFileUploader"] section{
  background:var(--surface-2)!important;border:1.5px dashed var(--text-5)!important;
  border-radius:10px!important;padding:20px!important;min-height:128px!important;transition:all .15s;
}
[data-testid="stFileUploader"] section:hover{border-color:var(--text-3)!important;background:var(--surface)!important;}
[data-testid="stFileUploaderDropzoneInstructions"]>div>span{font-size:13px!important;color:var(--text-2)!important;font-weight:500!important;}
[data-testid="stFileUploaderDropzoneInstructions"]>div>small{color:var(--text-4)!important;font-size:11.5px!important;}

/* slider */
.stSlider [data-baseweb="slider"] [role="slider"]{background:var(--text)!important;border-color:var(--surface)!important;}

/* ── BUTTONS ───────────────────────────── */
.stButton>button{
  background:var(--primary)!important;color:var(--primary-ink)!important;border:none!important;
  border-radius:8px!important;padding:10px 18px!important;font-size:13px!important;font-weight:500!important;
  font-family:var(--font-sans)!important;letter-spacing:.005em!important;
  transition:opacity .15s,transform .08s!important;cursor:pointer!important;box-shadow:var(--shadow-1)!important;
}
.stButton>button:hover:not(:disabled){opacity:.88!important;transform:translateY(-1px)!important;}
.stButton>button:active:not(:disabled){transform:translateY(0)!important;}
.stButton>button:disabled{background:var(--line)!important;color:var(--text-4)!important;cursor:not-allowed!important;box-shadow:none!important;}
.stButton>button[kind="secondary"]{
  background:var(--surface)!important;color:var(--text-2)!important;
  border:1px solid var(--line)!important;box-shadow:var(--shadow-3)!important;
}
.stButton>button[kind="secondary"]:hover:not(:disabled){background:var(--hover-bg)!important;border-color:var(--text-4)!important;opacity:1!important;}
[data-testid="stDownloadButton"]>button{
  background:var(--surface)!important;color:var(--text)!important;
  border:1px solid var(--line)!important;box-shadow:var(--shadow-3)!important;
}
[data-testid="stDownloadButton"]>button:hover{background:var(--hover-bg)!important;border-color:var(--text-3)!important;}

/* ── FILE PILL (custom) ────────────────── */
.fpill{display:flex;flex-direction:column;align-items:center;gap:6px;background:var(--surface);
  border:1px solid var(--line);border-radius:10px;padding:16px;min-height:128px;justify-content:center;text-align:center;}
.fpill .dot{width:8px;height:8px;border-radius:99px;background:var(--text);}
.fpill.muted .dot{background:var(--text-4);}
.fpill .name{font-size:13px;font-weight:500;color:var(--text);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.fpill .size{font-size:11.5px;color:var(--text-4);font-family:var(--font-mono);}

/* ── BANNERS ───────────────────────────── */
.bnr{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:10px;border:1px solid transparent;margin:14px 0;}
.bnr.ok{background:var(--ok-bg);border-color:var(--ok-br);}
.bnr.err{background:var(--err-bg);border-color:var(--err-br);}
.bnr.warn{background:var(--warn-bg);border-color:var(--warn-br);}
.bnr.info{background:var(--info-bg);border-color:var(--info-br);}
.bnr .ico{width:22px;height:22px;border-radius:99px;display:flex;align-items:center;justify-content:center;flex-shrink:0;color:#fff;font-size:12px;font-weight:700;margin-top:1px;}
.bnr.ok .ico{background:var(--ok-fg);} .bnr.err .ico{background:var(--err-fg);} .bnr.warn .ico{background:var(--warn-fg);} .bnr.info .ico{background:var(--info-fg);}
.bnr .t{font-size:13px;font-weight:600;}
.bnr.ok .t{color:var(--ok-fg);} .bnr.err .t{color:var(--err-fg);} .bnr.warn .t{color:var(--warn-fg);} .bnr.info .t{color:var(--info-fg);}
.bnr .s{font-size:12.5px;margin-top:2px;}
.bnr.ok .s{color:var(--ok-fg-2);} .bnr.err .s{color:var(--err-fg-2);} .bnr.warn .s{color:var(--warn-fg-2);} .bnr.info .s{color:var(--info-fg-2);}
.bnr .s .mono{font-family:var(--font-mono);font-size:12px;}

/* ── METRICS ───────────────────────────── */
.mg{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:14px 0 0;}
.mbox{background:var(--surface);border:1px solid var(--line-2);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow-2);}
.mbox .v{font-size:20px;font-weight:700;color:var(--text);line-height:1;}
.mbox .v.mono{font-size:18px;letter-spacing:-.5px;font-family:var(--font-mono);}
.mbox .l{font-size:10.5px;color:var(--text-4);text-transform:uppercase;letter-spacing:.07em;margin-top:6px;font-weight:500;}

/* ── STATUS pill ───────────────────────── */
.st-pill{display:inline-flex;align-items:center;gap:6px;padding:3px 9px 3px 8px;font-size:11.5px;font-weight:500;border-radius:99px;border:1px solid transparent;}
.st-pill .dot{width:6px;height:6px;border-radius:99px;background:currentColor;}
.st-pill.ok{background:var(--ok-bg);color:var(--ok-fg);border-color:var(--ok-br);}
.st-pill.err{background:var(--err-bg);color:var(--err-fg);border-color:var(--err-br);}
.st-pill.warn{background:var(--warn-bg);color:var(--warn-fg);border-color:var(--warn-br);}
.st-pill.run{background:var(--info-bg);color:var(--info-fg);border-color:var(--info-br);}
.st-pill.queued{background:var(--hover-bg);color:var(--text-3);border-color:var(--line);}

/* ── PROGRESS (custom) ─────────────────── */
.prg{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow-2);margin:14px 0;}
.prg-head{display:flex;justify-content:space-between;font-size:12.5px;color:var(--text-2);margin-bottom:8px;}
.prg-head .p{font-family:var(--font-mono);}
.prg-track{height:6px;background:var(--hover-bg);border-radius:99px;overflow:hidden;}
.prg-fill{height:100%;background:var(--text);border-radius:99px;transition:width .4s ease;}

/* ── DATAFRAME ─────────────────────────── */
[data-testid="stDataFrame"]{border:1px solid var(--line)!important;border-radius:10px!important;overflow:hidden!important;box-shadow:var(--shadow-2)!important;}
[data-testid="stDataFrame"] [role="columnheader"]{
  background:var(--surface-2)!important;color:var(--text-3)!important;
  font-size:11px!important;font-weight:500!important;text-transform:uppercase!important;letter-spacing:.07em!important;
  border-bottom:1px solid var(--line)!important;
}
[data-testid="stDataFrame"] [role="gridcell"]{font-size:12.5px!important;color:var(--text-2)!important;}

/* ── FORM CARD ─────────────────────────── */
[data-testid="stForm"]{
  background:var(--surface)!important;border:1px solid var(--line)!important;
  border-radius:12px!important;padding:20px!important;box-shadow:var(--shadow-2)!important;
}
[data-testid="stForm"] .stButton>button{width:100%;}
.fcard-title{font-size:13px;font-weight:600;color:var(--text);margin:0 0 14px;}

/* ── EMPTY ─────────────────────────────── */
.empty{background:var(--surface);border:1.5px dashed var(--line);border-radius:12px;
  padding:40px 28px;text-align:center;box-shadow:var(--shadow-3);max-width:520px;margin:8px auto;}
.empty .i{width:40px;height:40px;border-radius:10px;background:var(--hover-bg);color:var(--text-3);
  display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:18px;}
.empty .t{font-size:14px;font-weight:600;color:var(--text);}
.empty .s{font-size:12.5px;color:var(--text-4);margin-top:5px;}

/* ── spinner color ─────────────────────── */
.stSpinner>div{border-top-color:var(--text)!important;}

/* ── GLOSARIO (v2.4.1 — SaaS layout) ───── */
.gl-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:0 0 16px;}
.gl-stat{background:var(--surface);border:1px solid var(--line-2);border-radius:10px;padding:14px 16px;box-shadow:var(--shadow-2);}
.gl-stat .v{font-size:22px;font-weight:700;color:var(--text);line-height:1;letter-spacing:-.4px;}
.gl-stat .l{font-size:10.5px;color:var(--text-4);text-transform:uppercase;letter-spacing:.07em;margin-top:6px;font-weight:500;}

.gl-info{display:flex;gap:12px;align-items:flex-start;background:var(--info-bg);border:1px solid var(--info-br);border-radius:10px;padding:12px 14px;margin:0 0 22px;}
.gl-info .ico{width:22px;height:22px;border-radius:99px;background:var(--info-fg);color:#fff;font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-family:Georgia,serif;font-style:italic;margin-top:1px;}
.gl-info .t{font-size:13px;font-weight:600;color:var(--info-fg);}
.gl-info .s{font-size:12.5px;color:var(--info-fg-2);margin-top:3px;line-height:1.55;}

/* Cabecera de la tabla: grid de 5 columnas alineado con el st.columns de las filas */
.gl-thead-grid{
  display:grid;
  grid-template-columns:1.1fr 1.1fr 0.9fr 1.7fr 0.7fr;
  gap:14px;
  padding:10px 4px;
  color:var(--text-3);
  font-size:10.5px;
  font-weight:500;
  text-transform:uppercase;
  letter-spacing:.08em;
  border-bottom:1px solid var(--line);
  margin-top:6px;
}

/* Marcador para que las filas siguientes se compacten y separen tipo tabla */
.gl-rows-marker{display:none;}
[data-testid="stElementContainer"]:has(>.gl-rows-marker) ~ [data-testid="stElementContainer"]:has(>[data-testid="stHorizontalBlock"]){
  margin-top:0!important;
  margin-bottom:0!important;
}
[data-testid="stElementContainer"]:has(>.gl-rows-marker) ~ [data-testid="stElementContainer"]:has(>[data-testid="stHorizontalBlock"])>[data-testid="stHorizontalBlock"]{
  border-bottom:1px solid var(--line-2);
  padding:8px 4px;
}
[data-testid="stElementContainer"]:has(>.gl-rows-marker) ~ [data-testid="stElementContainer"]:has(>[data-testid="stHorizontalBlock"]):hover>[data-testid="stHorizontalBlock"]{
  background:var(--surface-2);
}

/* Tipografía de las celdas */
.gl-source{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.8px;}
.gl-target{color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.8px;}
.gl-note{color:var(--text-4);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

.gl-badge{display:inline-flex;align-items:center;padding:3px 9px;font-size:11px;font-weight:500;border-radius:99px;letter-spacing:.01em;}
.gl-badge.b-prop {background:#dbeafe;color:#1e40af;}
.gl-badge.b-term {background:#e5e7eb;color:#4b5563;}
.gl-badge.b-marca{background:#ede9fe;color:#6d28d9;}
.gl-badge.b-acro {background:#cffafe;color:#155e75;}
.gl-badge.b-idiom{background:#fed7aa;color:#c2410c;}
.gl-badge.b-slang{background:#fce7f3;color:#9f1239;}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# COMPONENTES HTML — funciones que devuelven markup
# ═══════════════════════════════════════════════════════════════════════════
def page_header(title: str, subtitle: str = "", right: str = ""):
    right_html = f'<div class="ph-r">{right}</div>' if right else ""
    st.markdown(f"""
    <div class="ph">
      <div>
        <h1 class="ph-t">{escape(title)}</h1>
        {f'<p class="ph-s">{escape(subtitle)}</p>' if subtitle else ""}
      </div>
      {right_html}
    </div>
    """, unsafe_allow_html=True)

def section_label(text: str, right: str = ""):
    right_html = f"<span>{right}</span>" if right else ""
    st.markdown(
        f'<div class="sl-row"><div class="sl">{escape(text)}</div>{right_html}</div>',
        unsafe_allow_html=True,
    )

def file_pill(name: str, size_bytes: int, muted: bool = False):
    size = f"{size_bytes/1024:.1f} KB" if size_bytes < 1_048_576 else f"{size_bytes/1_048_576:.1f} MB"
    cls = "fpill muted" if muted else "fpill"
    st.markdown(
        f'<div class="{cls}"><div class="dot"></div>'
        f'<div class="name">{escape(name)}</div>'
        f'<div class="size">{size}</div></div>',
        unsafe_allow_html=True,
    )

def banner(tone: str, title: str, body: str = "", body_html: Optional[str] = None):
    icon = {"ok": "✓", "err": "!", "warn": "!", "info": "i"}.get(tone, "•")
    content = body_html if body_html is not None else escape(body)
    st.markdown(f"""
    <div class="bnr {tone}">
      <div class="ico">{icon}</div>
      <div>
        <div class="t">{escape(title)}</div>
        <div class="s">{content}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

def metrics(items: list[tuple[str, str, bool]]):
    boxes = "".join(
        f'<div class="mbox"><div class="v {"mono" if mono else ""}">{escape(v)}</div>'
        f'<div class="l">{escape(l)}</div></div>'
        for v, l, mono in items
    )
    st.markdown(f'<div class="mg">{boxes}</div>', unsafe_allow_html=True)

def progress_bar(pct: int, label: str):
    st.markdown(f"""
    <div class="prg">
      <div class="prg-head"><span>{escape(label)}</span><span class="p">{pct}%</span></div>
      <div class="prg-track"><div class="prg-fill" style="width:{pct}%"></div></div>
    </div>
    """, unsafe_allow_html=True)

def empty_state(icon: str, title: str, sub: str):
    st.markdown(f"""
    <div class="empty">
      <div class="i">{icon}</div>
      <div class="t">{escape(title)}</div>
      <div class="s">{escape(sub)}</div>
    </div>
    """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# COLA DE TRADUCCIÓN (v3.8 — vive en el BACKEND: patrón 202+polling)
# ═══════════════════════════════════════════════════════════════════════════
# Antes: dict en cache_resource + worker thread que mantenía un request HTTP
# abierto hasta 3 h (frágil: un reinicio del frontend perdía la cola y dejaba
# jobs zombis; dos sesiones se pisaban el estado). Ahora POST /translate
# encola y responde en <1 s; el worker del BACKEND procesa uno a uno y la UI
# hace polling ligero cada 2,5 s. Visible para el usuario: la cola sobrevive
# a un F5 y a reinicios, el progreso es real (chunk actual/total) y cancelar
# funciona de verdad (el backend corta entre chunks).
import re as _re
import threading  # lo usan el chequeo de updates y el worker OCR
import uuid as _queue_uuid

_QUEUE_STATUS_MAP = {
    "queued":    "queued",
    "running":   "translating",
    "completed": "completed",
    "failed":    "failed",
    "cancelled": "cancelled",
}


def _my_job_uuids() -> list:
    """uuids encolados por ESTA sesión de navegador. Los activos se
    muestran globalmente (cola real del backend); esto solo decide qué
    terminados se listan como 'de esta sesión'."""
    if "my_job_uuids" not in st.session_state:
        st.session_state.my_job_uuids = []
    return st.session_state.my_job_uuids


def enqueue_translation(srt_file, context: str, target_lang: str, cpl_limit: int) -> str:
    """Encola un .srt en el backend. Devuelve el uuid del job.

    Lanza excepción si el backend rechaza el archivo o no responde: el
    caller la muestra al momento (antes un SRT corrupto fallaba minutos
    después, ya dentro de la cola)."""
    job_uuid = _queue_uuid.uuid4().hex[:12]
    r = requests.post(
        f"{BACKEND_URL}/translate",
        files={"file": (srt_file.name, srt_file.getvalue(), "text/plain")},
        data={
            "context":     context,
            "target_lang": target_lang,
            "cpl":         cpl_limit,
            "job_uuid":    job_uuid,
        },
        timeout=(10, 30),  # encolar es instantáneo: sin timeouts de horas
    )
    r.raise_for_status()
    _my_job_uuids().append(job_uuid)
    # Poda: la vista de sesión solo maneja los últimos 50; sin tope, una
    # sesión de semanas acumulaba uuids y bytes de SRT sin límite.
    st.session_state.my_job_uuids = st.session_state.my_job_uuids[-50:]
    vivos = set(st.session_state.my_job_uuids)
    cache = st.session_state.get("_q_result_cache", {})
    for u in [u for u in cache if u not in vivos]:
        cache.pop(u, None)
    return job_uuid


def _job_to_ui(j: dict) -> dict:
    """Mapea el Job del backend al shape que renderiza la cola."""
    done = j.get("status") == "completed"
    return {
        "id":           j.get("job_uuid") or str(j["id"]),
        "backend_id":   j["id"],
        "srt_name":     j.get("filename", "—"),
        "status":       _QUEUE_STATUS_MAP.get(j.get("status"), j.get("status")),
        "started_at":   j.get("started_at"),
        "completed_at": j.get("finished_at"),
        "error":        j.get("error") or "",
        "failed_cues":  j.get("failed_cues", 0),
        "target_lang":  j.get("target_lang", "es"),
        "result_bytes": None,   # se rellena bajo demanda para completados
        "result_name":  f"{j.get('target_lang', 'es')}_{j.get('filename', 'traducido.srt')}",
        "metrics": {
            "cpl_rate": f"{j.get('cpl_compliance', 0.0):.1f}%",
            "lines":    str(j.get("n_translations", 0)),
            "langs":    f"→ {j.get('target_lang', 'es').upper()}",
        } if done else None,
    }


def _result_bytes_cached(backend_id: int, job_uuid: str):
    """Descarga (una sola vez) el SRT final de un job completado y lo
    cachea en la sesión — el autorefresh no debe re-descargar cada 2,5 s.

    Los fallos también se acotan: 3 intentos y se rinde (el render
    ofrece el Historial como vía) — sin tope, un backend caído se
    reintentaba en cada tick bloqueando el render."""
    cache = st.session_state.setdefault("_q_result_cache", {})
    if job_uuid in cache:
        return cache[job_uuid]
    fails = st.session_state.setdefault("_q_result_fails", {})
    if fails.get(job_uuid, 0) >= 3:
        return None
    try:
        r = requests.get(
            f"{BACKEND_URL}/jobs/{backend_id}/download", timeout=10,
        )
        r.raise_for_status()
        cache[job_uuid] = r.content
        fails.pop(job_uuid, None)
        return cache[job_uuid]
    except Exception:
        fails[job_uuid] = fails.get(job_uuid, 0) + 1
        return None


def get_queue_snapshot() -> dict:
    """Snapshot de la cola desde el backend (1-2 GETs ligeros).

    - Activos (queued/running): TODOS los del backend — un F5 o una
      segunda pestaña ven la cola real, no una copia en memoria.
    - Terminales: solo los de esta sesión (el resto vive en Historial).
    """
    mine = _my_job_uuids()
    jobs: dict = {}
    try:
        r = requests.get(f"{BACKEND_URL}/jobs/active", timeout=5)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            jobs[j.get("job_uuid") or str(j["id"])] = j
        if mine:
            r = requests.get(
                f"{BACKEND_URL}/jobs/by-uuids",
                params={"uuids": ",".join(mine[-50:])},
                timeout=5,
            )
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                jobs[j.get("job_uuid") or str(j["id"])] = j
    except Exception:
        return {"pending": [], "current": None, "completed": [], "offline": True}

    pending, current, completed = [], None, []
    for j in sorted(jobs.values(), key=lambda x: x["id"]):
        ui = _job_to_ui(j)
        if ui["status"] == "queued":
            pending.append(ui)
        elif ui["status"] == "translating":
            current = ui
        elif ui["id"] in mine:
            if ui["status"] == "completed":
                ui["result_bytes"] = _result_bytes_cached(ui["backend_id"], ui["id"])
            completed.append(ui)
    return {"pending": pending, "current": current, "completed": completed, "offline": False}


def clear_completed_jobs() -> None:
    """Quita los terminados de la vista de esta sesión. No borra nada del
    backend: siguen disponibles en el Historial."""
    terminados = set()
    try:
        r = requests.get(
            f"{BACKEND_URL}/jobs/by-uuids",
            params={"uuids": ",".join(_my_job_uuids()[-50:])},
            timeout=5,
        )
        for j in r.json().get("jobs", []):
            if j.get("status") in ("completed", "failed", "cancelled"):
                terminados.add(j.get("job_uuid"))
    except Exception:
        pass
    st.session_state.my_job_uuids = [
        u for u in _my_job_uuids() if u not in terminados
    ]
    st.session_state.pop("_q_result_cache", None)


def cancel_job(job_id: str) -> str:
    """Cancela un job por uuid contra el backend.

    'queued' se quita de la cola; 'running' se corta al terminar el
    chunk en curso (el request a OpenAI en vuelo ya está pagado)."""
    try:
        r = requests.post(
            f"{BACKEND_URL}/jobs/by-uuid/{job_id}/cancel", timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "queued":  "cancelled_pending",
            "running": "cancelled_in_progress",
        }.get(data.get("was", ""), "already_done")
    except Exception:
        return "not_found"


def _fetch_job_logs(job_uuid: str, since: int = 0) -> list[dict]:
    """GET de logs del backend para el job_uuid dado, desde el seq `since`.

    Tolerante a fallos: si el backend está caído o el endpoint no existe
    todavía, devuelve [] silenciosamente — no queremos romper la UI por
    un componente accesorio. El error se loggeará indirectamente porque
    el job seguirá mostrándose sin logs.
    """
    if not job_uuid:
        return []
    try:
        r = requests.get(
            f"{BACKEND_URL}/jobs/by-uuid/{job_uuid}/logs",
            params={"since": since},
            timeout=3,
        )
        r.raise_for_status()
        return r.json().get("logs", [])
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# CHEQUEO DE ACTUALIZACIÓN (en segundo plano, sin tocar la latencia de render)
# ═══════════════════════════════════════════════════════════════════════════
_UPDATE_CHECK_EVERY_S = 6 * 3600  # re-chequear cada 6 h


@st.cache_resource
def _update_check_state() -> dict:
    """Estado compartido del chequeo de versión (singleton por proceso)."""
    return {"local": None, "remote": None, "last_check": 0.0, "checking": False}


def _maybe_check_updates() -> dict:
    """Compara la versión desplegada con la publicada en GitHub.

    El chequeo corre en un thread aparte y se relanza como mucho cada
    _UPDATE_CHECK_EVERY_S: el render NUNCA espera por red, y una
    instalación que lleve semanas encendida sigue enterándose de las
    versiones nuevas (el diseño anterior chequeaba solo al arrancar).
    """
    state = _update_check_state()
    now = time.time()
    if state["checking"] or now - state["last_check"] < _UPDATE_CHECK_EVERY_S:
        return state
    state["checking"] = True

    def _worker() -> None:
        import re as _re
        import sys as _sys
        try:
            r = requests.get(f"{BACKEND_URL}/", timeout=5)
            state["local"] = r.json().get("version")
        except Exception as e:
            print(f"[update-check] backend inaccesible: {e}", file=_sys.stderr)
        try:
            r = requests.get(
                "https://raw.githubusercontent.com/AlejandroAraque/"
                "subtitulam-ai/main/pyproject.toml",
                timeout=5,
            )
            m = _re.search(r'^version\s*=\s*"([^"]+)"', r.text, _re.M)
            if m:
                state["remote"] = m.group(1)
        except Exception as e:
            print(f"[update-check] GitHub inaccesible: {e}", file=_sys.stderr)
        state["last_check"] = time.time()
        state["checking"] = False
        print(f"[update-check] local={state['local']} remote={state['remote']}",
              file=_sys.stderr)

    threading.Thread(target=_worker, daemon=True).start()
    return state


def _version_tuple(v: str | None) -> tuple | None:
    try:
        return tuple(int(x) for x in v.split("."))
    except (AttributeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════
# brand con logo más refinado
with st.sidebar:
    st.markdown("""
    <div class="sb-brand">
      <div class="sb-logo">◆</div>
      <div class="sb-text">
        <div class="sb-name">Subtitulam</div>
        <div class="sb-tag">AI Subtitle Platform</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    PAGES = {
        "Nueva Traducción":  "workspace",
        "Glosario y Reglas": "glosario",
        "Historial":         "historial",
        "Preview":           "preview",
    }
    labels = list(PAGES.keys())

    # Radio enlazado directamente a session_state mediante `key`.
    # Evita el conflicto de doble-click que aparece cuando se pasa `index=`
    # y a la vez se sobreescribe `st.session_state.page` después.
    if "nav_label" not in st.session_state:
        st.session_state.nav_label = next(
            (k for k, v in PAGES.items() if v == st.session_state.page),
            labels[0],
        )

    selected = st.radio("nav", labels, key="nav_label", label_visibility="collapsed")
    st.session_state.page = PAGES[selected]

    # Versión desplegada + aviso de actualización (chequeo en background;
    # si aún no terminó o no hay red, muestra el fallback sin aviso).
    _upd = _maybe_check_updates()
    _v_local = _upd.get("local")
    _v_remote = _upd.get("remote")
    _vl, _vr = _version_tuple(_v_local), _version_tuple(_v_remote)
    if _vl and _vr and _vr > _vl:
        st.markdown(
            f'<div class="sb-foot">'
            f'<div style="font-size:11px;color:var(--warn-fg);font-weight:600;">'
            f'⬆ Actualización disponible (v{escape(_v_remote)})</div>'
            f'<div class="dim">GPT-4o · v{escape(_v_local)}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        if st.session_state.get("_update_requested"):
            st.markdown(
                '<div class="dim" style="font-size:10.5px;">Actualización en '
                'camino: la app se reiniciará sola en unos minutos. Después, '
                'cierra esta pestaña y abre una nueva.</div>',
                unsafe_allow_html=True,
            )
        elif st.button("⬆ Actualizar ahora", key="btn_request_update",
                       use_container_width=True, type="secondary"):
            try:
                r = requests.post(f"{BACKEND_URL}/system/request-update", timeout=5)
                r.raise_for_status()
                st.session_state._update_requested = True
                st.rerun()
            except requests.exceptions.RequestException:
                st.toast(
                    "No se pudo solicitar la actualización (¿backend caído?). "
                    "Alternativa: ejecutar scripts\\actualizar.ps1",
                    icon="⚠️",
                )
    else:
        _v_txt = f"v{escape(_v_local)}" if _v_local else "v3.5"
        st.markdown(
            f'<div class="sb-foot"><div class="dim">GPT-4o · {_v_txt}</div></div>',
            unsafe_allow_html=True,
        )

# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · WORKSPACE
# ═══════════════════════════════════════════════════════════════════════════
def _render_translation_queue() -> bool:
    """Renderiza el panel de cola de traducción si hay algo activo o
    completado en la sesión. Devuelve True si hay actividad (current o
    pending) — la UI usa este flag para auto-refrescar.
    """
    snap = get_queue_snapshot()

    # Backend caído: la cola NO se pierde (vive en el backend); se avisa
    # y se sigue refrescando para reconectar solo.
    if snap.get("offline"):
        if _my_job_uuids():
            st.warning(
                "Sin conexión con el motor. La cola está a salvo en el "
                "backend y esta vista se reconectará sola.",
                icon="⚠️",
            )
            return True  # seguir auto-refrescando para reconectar
        return False

    has_activity = bool(snap["current"] or snap["pending"])
    has_any = has_activity or bool(snap["completed"])

    # Al detectar un job recién completado, invalidar la caché del Historial:
    # sin esto, la pestaña Historial mostraba la lista pre-traducción hasta el
    # siguiente encolado (el worker corre en un thread y no puede tocar la
    # caché de Streamlit con seguridad; se hace aquí, en el hilo de render).
    n_done = len(snap["completed"])
    if st.session_state.get("_q_done_seen", 0) != n_done:
        st.session_state._q_done_seen = n_done
        api_get_jobs.clear()

    if not has_any:
        return False

    st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
    section_label(
        "Cola de traducción",
        right=(
            '<span class="mono" style="color:var(--text-4);font-size:11.5px;">'
            f'{len(snap["pending"])} pendiente(s) · '
            f'{1 if snap["current"] else 0} en curso · '
            f'{len(snap["completed"])} completado(s)</span>'
        ),
    )

    # JOB EN CURSO ────────────────────────────────────────────────────
    cur = snap["current"]
    if cur:
        started = cur.get("started_at")
        elapsed_s = 0.0
        if started:
            try:
                t0 = datetime.fromisoformat(started)
                elapsed_s = (datetime.utcnow() - t0).total_seconds()
            except Exception:
                pass
        elapsed_str = f"{int(elapsed_s // 60)}:{int(elapsed_s % 60):02d}"

        # Progreso REAL desde los logs del backend ("Chunk 12/84"). Si aún
        # no hay chunks (arrancando), cae a la estimación temporal antigua.
        logs = _fetch_job_logs(cur["id"], since=0)
        pct = None
        pct_label = "GPT-4o + RAG + glosario…"
        for entry in reversed(logs):
            m = _re.search(r"Chunk (\d+)/(\d+)", entry.get("message", ""))
            if m:
                done_c, total_c = int(m.group(1)), int(m.group(2))
                pct = int(done_c / max(1, total_c) * 100)
                pct_label = f"Chunk {done_c}/{total_c} · GPT-4o + RAG + glosario"
                break
        if pct is None:
            expected_s = max(60.0, min(900.0, elapsed_s * 1.5 + 60))
            pct = int(min(95, elapsed_s / expected_s * 100))

        info_col, cancel_col = st.columns([5, 0.6], gap="small")
        with info_col:
            st.markdown(
                f'<div style="font-size:12.5px;color:var(--text-2);'
                f'margin-bottom:4px;display:flex;justify-content:space-between;">'
                f'<span><b style="color:var(--info-fg);">🔄 Traduciendo</b>  '
                f'{escape(cur["srt_name"])}</span>'
                f'<span class="mono" style="color:var(--text-4);">'
                f'{elapsed_str}</span></div>',
                unsafe_allow_html=True,
            )
            progress_bar(pct, pct_label)
        with cancel_col:
            st.markdown('<div style="height:6px;"></div>', unsafe_allow_html=True)
            if st.button(
                "✖", key=f"qcancel_cur_{cur['id']}",
                type="secondary", use_container_width=True,
                help="Cancelar este job. El coste OpenAI del request "
                     "en curso ya está consumido, pero el resultado se "
                     "descarta y la cola sigue con el siguiente.",
            ):
                cancel_job(cur["id"])
                st.toast(f"Cancelando '{cur['srt_name']}'…", icon="✖")
                st.rerun()

        # LOGS EN VIVO ────────────────────────────────────────────────
        # (obtenidos arriba, reutilizados por la barra de progreso real)
        with st.expander(
            f"📜 Logs en vivo · {len(logs)} eventos",
            expanded=bool(logs),
        ):
            if not logs:
                st.markdown(
                    '<div style="font-size:12px;color:var(--text-4);font-style:italic;">'
                    'Esperando primer evento del backend…</div>',
                    unsafe_allow_html=True,
                )
            else:
                rows = []
                for entry in logs[-60:]:  # últimos 60 cabe en la UI sin scroll bruto
                    ts = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
                    color = {
                        "info":  "var(--text-2)",
                        "warn":  "var(--warn-fg)",
                        "error": "var(--err-fg-2)",
                    }.get(entry.get("level", "info"), "var(--text-2)")
                    msg = escape(entry["message"])
                    rows.append(
                        f'<div style="font-family:var(--font-mono);font-size:11.5px;'
                        f'line-height:1.5;color:{color};">'
                        f'<span style="color:var(--text-4);">{ts}</span>  '
                        f'{msg}</div>'
                    )
                st.markdown(
                    '<div style="max-height:240px;overflow-y:auto;'
                    'background:var(--bg);padding:8px 10px;border-radius:6px;'
                    'border:1px solid var(--line);">'
                    + "".join(rows)
                    + '</div>',
                    unsafe_allow_html=True,
                )

    # PENDIENTES ──────────────────────────────────────────────────────
    if snap["pending"]:
        for i, p in enumerate(snap["pending"]):
            info_col, pos_col, cancel_col = st.columns(
                [4.5, 0.7, 0.5], gap="small",
            )
            with info_col:
                st.markdown(
                    f'<div style="font-size:12.5px;color:var(--text-3);'
                    f'margin-top:8px;">⏳ En cola  '
                    f'{escape(p["srt_name"])}</div>',
                    unsafe_allow_html=True,
                )
            with pos_col:
                st.markdown(
                    f'<div class="mono" style="text-align:right;'
                    f'color:var(--text-4);font-size:11.5px;margin-top:10px;">'
                    f'pos. {i + 1}</div>',
                    unsafe_allow_html=True,
                )
            with cancel_col:
                st.markdown('<div style="height:4px;"></div>', unsafe_allow_html=True)
                if st.button(
                    "✖", key=f"qcancel_pend_{p['id']}",
                    type="secondary", use_container_width=True,
                    help="Quitar este job de la cola antes de que empiece",
                ):
                    cancel_job(p["id"])
                    st.toast(f"'{p['srt_name']}' quitado de la cola.",
                             icon="✖")
                    st.rerun()

    # COMPLETADOS DE LA SESIÓN ────────────────────────────────────────
    if snap["completed"]:
        st.markdown(
            '<div style="font-size:11px;color:var(--text-4);'
            'text-transform:uppercase;letter-spacing:.07em;'
            'margin:14px 0 6px;font-weight:500;">'
            'Completados en esta sesión</div>',
            unsafe_allow_html=True,
        )
        for c in reversed(snap["completed"]):  # más recientes primero
            t0 = c.get("started_at")
            t1 = c.get("completed_at")
            took_str = ""
            if t0 and t1:
                try:
                    dt = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds()
                    took_str = f" · {int(dt // 60)}:{int(dt % 60):02d}"
                except Exception:
                    pass
            if c["status"] == "completed":
                # Línea con botón de descarga + botón ver en preview
                colL, colDL, colPV = st.columns([3.5, 1.2, 0.9], gap="small")
                with colL:
                    m = c.get("metrics") or {}
                    n_failed = c.get("failed_cues", 0)
                    # Con cues fallidas el job sigue siendo descargable,
                    # pero el estado deja de ser un ✅ limpio: hay [ERROR]
                    # dentro del SRT que el revisor debe buscar y arreglar.
                    if n_failed:
                        estado = (f'<b style="color:var(--warn-fg);">⚠ Listo '
                                  f'con {n_failed} error(es)</b>')
                    else:
                        estado = '<b style="color:var(--ok-fg);">✅ Listo</b>'
                    st.markdown(
                        f'<div style="font-size:12.5px;color:var(--text-2);'
                        f'margin-top:6px;">'
                        f'{estado}  '
                        f'{escape(c["srt_name"])}'
                        f'<span class="mono" style="color:var(--text-4);'
                        f'margin-left:8px;">{escape(m.get("lines", "—"))} líneas · '
                        f'CPL {escape(m.get("cpl_rate", "—"))}{took_str}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    if n_failed:
                        st.markdown(
                            '<div style="font-size:11.5px;color:var(--warn-fg);'
                            'margin-top:2px;">Busca "[ERROR]" en el SRT '
                            'descargado: esas cues conservan el inglés original.'
                            '</div>',
                            unsafe_allow_html=True,
                        )
                with colDL:
                    if c.get("result_bytes"):
                        st.download_button(
                            "↓ .srt",
                            data=c["result_bytes"],
                            file_name=c["result_name"],
                            mime="text/plain",
                            use_container_width=True,
                            key=f"qdl_{c['id']}",
                        )
                    else:
                        # Descarga directa no disponible (fallo transitorio
                        # agotado): el archivo sigue en el backend.
                        st.markdown(
                            '<div style="font-size:11px;color:var(--text-4);'
                            'margin-top:10px;text-align:center;">Descárgalo '
                            'en Historial</div>',
                            unsafe_allow_html=True,
                        )
                with colPV:
                    # Sin botón "Preview" falso: Streamlit no permite
                    # pre-cargar un file_uploader, así que un botón que solo
                    # muestra un toast es un dead-end que confunde. El flujo
                    # honesto es descargar y subir en la pestaña Preview.
                    st.markdown(
                        '<div style="font-size:11px;color:var(--text-4);'
                        'margin-top:10px;text-align:center;">Revísalo en '
                        'Preview</div>',
                        unsafe_allow_html=True,
                    )
            elif c["status"] == "cancelled":
                st.markdown(
                    f'<div style="font-size:12.5px;color:var(--text-4);'
                    f'margin:6px 0;">'
                    f'<b style="color:var(--text-3);">✖ Cancelado</b>  '
                    f'<span style="text-decoration:line-through;">'
                    f'{escape(c["srt_name"])}</span>{took_str}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            else:  # failed
                st.markdown(
                    f'<div style="font-size:12.5px;color:var(--err-fg-2);'
                    f'margin:6px 0;">'
                    f'<b style="color:var(--err-fg);">✖ Falló</b>  '
                    f'{escape(c["srt_name"])}{took_str}<br>'
                    f'<span class="mono" style="font-size:11px;">'
                    f'{escape(c.get("error", ""))}</span></div>',
                    unsafe_allow_html=True,
                )

        # Botón limpiar historial de sesión
        colCL, _ = st.columns([1, 4])
        with colCL:
            if st.button("Limpiar historial", type="secondary",
                         use_container_width=True, key="qclear"):
                clear_completed_jobs()
                st.rerun()

    return has_activity


def render_workspace():
    page_header(
        "Nueva Traducción",
        "Sube tus .srt y tradúcelos con IA. Puedes encolar varios — se procesan uno a uno.",
    )

    # ── COLA DE TRADUCCIÓN (arriba de todo) ──────────────────────────
    queue_active = _render_translation_queue()

    # Contexto
    section_label("Contexto global")
    st.session_state.context_global = st.text_input(
        "ctx",
        value=st.session_state.context_global,
        placeholder='Ej: "Breaking Bad" — serie dramática. Walter White, protagonista. No adaptar nombres propios.',
        label_visibility="collapsed",
    )
    st.markdown('<div class="hint">Este texto se usará como contexto para mejorar la coherencia narrativa. Aplica a TODOS los .srt que encoles juntos.</div>', unsafe_allow_html=True)

    # Archivos — ahora multi-upload
    section_label("Archivos")
    col_srt, col_mp4 = st.columns(2, gap="medium")
    with col_srt:
        st.markdown('<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">Subtítulos .srt <span style="color:#ef4444;">*</span> <em style="color:var(--text-4);font-style:normal;">(uno o varios)</em></div>', unsafe_allow_html=True)
        # Key rotatoria: Streamlit no vacía el uploader tras encolar; sin
        # rotarla, un segundo click re-POSTeaba TODOS los archivos
        # (jobs duplicados = 30-60 min de cola y ~1 € cada uno).
        st.session_state.setdefault("srt_up_gen", 0)
        srt_files = st.file_uploader(
            "srt",
            type=["srt"],
            label_visibility="collapsed",
            key=f"srt_up_{st.session_state.srt_up_gen}",
            accept_multiple_files=True,
        )
        if srt_files:
            for f in srt_files:
                file_pill(f.name, f.size)
    with col_mp4:
        st.markdown('<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">Vídeo de referencia .mp4 <em style="color:var(--text-4);font-style:normal;">(opcional, no se traduce)</em></div>', unsafe_allow_html=True)
        mp4_file = st.file_uploader("mp4", type=["mp4"], label_visibility="collapsed", key="mp4_up")
        if mp4_file:
            file_pill(mp4_file.name, mp4_file.size, muted=True)

    # Configuración
    col_lang, col_cpl = st.columns(2, gap="medium")
    with col_lang:
        lang_opts = {"es": "Español (ES)", "fr": "Francés (FR)", "de": "Alemán (DE)", "pt": "Portugués (PT)", "it": "Italiano (IT)"}
        idx = list(lang_opts).index(st.session_state.target_lang)
        choice = st.selectbox("Idioma de destino", list(lang_opts.values()), index=idx)
        st.session_state.target_lang = [k for k, v in lang_opts.items() if v == choice][0]
    with col_cpl:
        st.session_state.cpl_limit = st.slider("Límite CPL (caracteres por línea)", 30, 50, st.session_state.cpl_limit)
        st.markdown(
            f'<div class="hint">Normas habituales: <b>{CPL_UNE} = UNE 153010</b> '
            f'(TV España) · {CPL_NETFLIX} = Netflix.</div>',
            unsafe_allow_html=True,
        )

    # CTA — ahora encola en lugar de procesar síncrono
    st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
    label = (
        f"Encolar {len(srt_files)} traducción(es)  →"
        if srt_files else
        "Encolar traducción  →"
    )
    clicked = st.button(
        label,
        disabled=not srt_files,
        use_container_width=True,
    )

    # Mensajes de rechazo del encolado anterior (sobreviven al rerun)
    for msg in st.session_state.pop("_enqueue_errors", []):
        st.error(msg, icon="🚫")

    if clicked and srt_files:
        encolados, rechazados = 0, []
        for f in srt_files:
            try:
                enqueue_translation(
                    f,
                    st.session_state.context_global,
                    st.session_state.target_lang,
                    st.session_state.cpl_limit,
                )
                encolados += 1
            except requests.HTTPError as e:
                # El backend valida ANTES de encolar (SRT corrupto, no
                # UTF-8, >5MB…): mostrar su mensaje, no un traceback.
                try:
                    detalle = e.response.json().get("detail", str(e))
                except Exception:
                    detalle = str(e)
                rechazados.append(f"**{f.name}**: {detalle}")
            except Exception:
                rechazados.append(
                    f"**{f.name}**: sin conexión con el motor "
                    f"(¿backend arrancando?). Vuelve a intentarlo."
                )
        # Invalidar cache de jobs para que aparezcan en el historial cuando
        # el backend los persista
        api_get_jobs.clear()
        # Rerun SIEMPRE, con el uploader rotado: dejar los archivos en el
        # uploader tras encolar invitaba al doble click (jobs duplicados).
        # Los errores se muestran tras el rerun (session_state) y la cola
        # recién encolada aparece con su autorefresh.
        st.session_state.srt_up_gen += 1
        st.session_state._enqueue_errors = rechazados
        if encolados:
            st.toast(f"Encolado(s) {encolados} archivo(s) en cola.", icon="✅")
        st.rerun()

    # Auto-refresh vía streamlit-autorefresh (componente JS, no bloquea
    # el render principal de Streamlit). Sustituye al patrón previo
    # `time.sleep + st.rerun` que causaba re-renders en cascada y
    # widgets de otras páginas filtrándose en el Workspace.
    if queue_active:
        st_autorefresh(interval=2500, key="cola_autorefresh")

# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · GLOSARIO  (v2.4.1 — layout SaaS con modales)
# ═══════════════════════════════════════════════════════════════════════════
_GL_CATEGORIES = ["nombre-propio", "marca", "acrónimo", "slang", "idiom", "término"]

# Mapeo de category → clase CSS ASCII-safe para el badge.
_CAT_BADGE = {
    "nombre-propio": "b-prop",
    "marca":         "b-marca",
    "acrónimo":      "b-acro",
    "slang":         "b-slang",
    "idiom":         "b-idiom",
    "término":       "b-term",
}


# ── Modales (st.dialog requiere registro a nivel de módulo) ────────────────
@st.dialog("Añadir término al glosario")
def _dialog_add_term():
    with st.form("dlg_add_term", clear_on_submit=True):
        source = st.text_input("Término original",       placeholder="Ej: Walter White")
        target = st.text_input("Traducción obligatoria", placeholder="Ej: Walter White")
        note   = st.text_area ("Contexto de uso (opcional)",
                               placeholder="Ej: No traducir, nombre propio del protagonista.",
                               height=90)
        cat    = st.selectbox ("Etiqueta", _GL_CATEGORIES)
        c1, c2 = st.columns(2)
        cancel = c1.form_submit_button("Cancelar", type="secondary", use_container_width=True)
        save   = c2.form_submit_button("Añadir término",                use_container_width=True)

    if cancel:
        st.rerun()
    if save:
        if not source.strip() or not target.strip():
            st.error("Término y Traducción son obligatorios.")
            return
        created = api_post_glossary(source.strip(), target.strip(), cat, note.strip())
        if created is not None:
            api_get_glossary.clear()
            st.toast(f'Añadido: {created["source"]} → {created["target"]}', icon="✅")
            st.rerun()


@st.dialog("Editar término")
def _dialog_edit_term(existing: dict):
    cat_now = existing.get("category", "término")
    cat_idx = _GL_CATEGORIES.index(cat_now) if cat_now in _GL_CATEGORIES else len(_GL_CATEGORIES) - 1

    with st.form(f"dlg_edit_term_{existing['id']}"):
        source = st.text_input("Término original",       value=existing.get("source", ""))
        target = st.text_input("Traducción obligatoria", value=existing.get("target", ""))
        note   = st.text_area ("Contexto de uso", value=existing.get("note", "") or "", height=90)
        cat    = st.selectbox ("Etiqueta", _GL_CATEGORIES, index=cat_idx)
        c1, c2 = st.columns(2)
        cancel = c1.form_submit_button("Cancelar", type="secondary", use_container_width=True)
        save   = c2.form_submit_button("Guardar cambios",                use_container_width=True)

    if cancel:
        st.rerun()
    if save:
        if not source.strip() or not target.strip():
            st.error("Término y Traducción son obligatorios.")
            return
        # El backend no expone PUT — borrar y recrear es atómico a nivel UX.
        if api_delete_glossary(existing["id"]):
            created = api_post_glossary(source.strip(), target.strip(), cat, note.strip())
            if created is not None:
                api_get_glossary.clear()
                st.toast(f'Actualizado: {created["source"]} → {created["target"]}', icon="✅")
                st.rerun()


@st.dialog("Importar / exportar CSV")
def _dialog_csv(glossary: list[dict]):
    st.markdown(
        '<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">'
        'Descargar glosario actual</div>',
        unsafe_allow_html=True,
    )
    csv_bytes = _build_glossary_csv(glossary)
    st.download_button(
        label=f"⬇  glossary.csv ({len(glossary)} términos)",
        data=csv_bytes,
        file_name="glossary.csv",
        mime="text/csv",
        use_container_width=True,
        disabled=(len(glossary) == 0),
        key="dlg_csv_dl",
    )
    st.markdown(
        '<div style="font-size:11.5px;color:var(--text-4);margin:6px 0 18px;">'
        'Compatible con Excel (UTF-8 + ;). Edita las filas y vuelve a subir el archivo.'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">'
        'Subir CSV con términos</div>',
        unsafe_allow_html=True,
    )
    csv_file = st.file_uploader("csv", type=["csv"], label_visibility="collapsed",
                                key="dlg_csv_upload")

    c1, c2 = st.columns(2)
    if c1.button("Cerrar", type="secondary", use_container_width=True, key="dlg_csv_close"):
        st.rerun()
    if c2.button("Importar al glosario",
                 disabled=(csv_file is None),
                 use_container_width=True,
                 key="dlg_csv_import"):
        result = api_import_glossary_csv(csv_file.name, csv_file.getvalue())
        api_get_glossary.clear()
        st.session_state["glossary_import_result"] = result
        st.rerun()


def render_glosario():
    glossary = api_get_glossary()
    n = len(glossary)

    # ── HEADER con acciones a la derecha ───────────────────────────────
    head_l, head_r = st.columns([3, 1.4], gap="small")
    with head_l:
        st.markdown("""
        <div style="margin-bottom:24px;padding-bottom:18px;border-bottom:1px solid var(--line-2);">
          <h1 class="ph-t">Glosario y reglas RAG</h1>
          <p class="ph-s">Términos con traducción fija. La IA los aplicará como contexto prioritario, por encima de los ejemplos vectoriales.</p>
        </div>
        """, unsafe_allow_html=True)
    with head_r:
        st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
        b1, b2 = st.columns(2, gap="small")
        if b1.button("+ Añadir", use_container_width=True, key="gl_btn_add"):
            _dialog_add_term()
        if b2.button("⇅ CSV", type="secondary", use_container_width=True, key="gl_btn_csv"):
            _dialog_csv(glossary)

    # ── Banner del último import (si existe), de un solo uso ───────────
    res = st.session_state.pop("glossary_import_result", None)
    if res:
        if res.get("ok"):
            n_imp = res.get("imported", 0)
            n_sk  = res.get("skipped", 0)
            errs  = res.get("errors", []) or []
            if errs:
                err_html = "<br>".join(escape(e) for e in errs[:6])
                if len(errs) > 6:
                    err_html += f"<br>… y {len(errs)-6} más"
                banner("warn",
                       f"Importados {n_imp} · omitidos {n_sk} · {len(errs)} fila(s) con error",
                       body_html=err_html)
            elif n_imp > 0:
                banner("ok", f"Importados {n_imp} términos nuevos",
                       body=f"Se omitieron {n_sk} duplicados ya existentes."
                            if n_sk else "Sin duplicados detectados.")
            else:
                banner("warn", "Sin términos nuevos",
                       body=f"Las {n_sk} filas ya estaban en el glosario.")
        else:
            banner("err", "Error al importar", body=res.get("detail", "Sin detalle."))

    # ── STATS (4 tarjetas) ─────────────────────────────────────────────
    n_propio = sum(1 for t in glossary if t.get("category") == "nombre-propio")
    n_term   = sum(1 for t in glossary if t.get("category") == "término")
    n_idiom  = sum(1 for t in glossary if t.get("category") in ("idiom", "slang"))
    st.markdown(f"""
    <div class="gl-stats">
      <div class="gl-stat"><div class="v">{n}</div><div class="l">Términos totales</div></div>
      <div class="gl-stat"><div class="v">{n_propio}</div><div class="l">Nombres propios</div></div>
      <div class="gl-stat"><div class="v">{n_term}</div><div class="l">Términos generales</div></div>
      <div class="gl-stat"><div class="v">{n_idiom}</div><div class="l">Idioms y slang</div></div>
    </div>
    """, unsafe_allow_html=True)

    # ── INFO BOX (cómo se usa) ─────────────────────────────────────────
    st.markdown("""
    <div class="gl-info">
      <div class="ico">i</div>
      <div>
        <div class="t">Cómo se usa en la traducción</div>
        <div class="s">Cada término se inyecta como regla obligatoria en el system prompt antes de cada batch. La IA debe respetarlo aunque RAG sugiera otra cosa — el glosario tiene prioridad sobre los ejemplos vectoriales.</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if n == 0:
        empty_state("📖", "Glosario vacío",
                    "Añade tu primer término con “+ Añadir” o importa un CSV existente.")
        return

    # ── FILTROS ────────────────────────────────────────────────────────
    f1, f2 = st.columns([3, 1.4], gap="medium")
    with f1:
        q = st.text_input("Buscar",
                          placeholder="🔍   Buscar término, traducción o contexto…",
                          key="gl_search", label_visibility="collapsed")
    with f2:
        cat_filter = st.selectbox("Etiqueta", ["Todas las etiquetas"] + _GL_CATEGORIES,
                                  key="gl_cat_filter", label_visibility="collapsed")

    filtered = glossary
    if q.strip():
        ql = q.strip().lower()
        filtered = [t for t in filtered if (
            ql in (t.get("source","") or "").lower()
            or ql in (t.get("target","") or "").lower()
            or ql in (t.get("note","") or "").lower()
        )]
    if cat_filter != "Todas las etiquetas":
        filtered = [t for t in filtered if t.get("category") == cat_filter]

    # ── Encabezado de la tabla con conteo ──────────────────────────────
    st.markdown(
        f'<div class="sl-row" style="margin-top:14px;">'
        f'<div class="sl">Reglas activas</div>'
        f'<span class="mono" style="color:var(--text-4);font-size:12px;">'
        f'{len(filtered)} de {n}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not filtered:
        empty_state("🔎", "Sin resultados",
                    "Cambia el filtro o borra la búsqueda.")
        return

    # ── TABLA: header HTML + filas con botones por fila ───────────────
    st.markdown("""
    <div class="gl-thead-grid">
      <div>Término</div>
      <div>Traducción</div>
      <div>Etiqueta</div>
      <div>Contexto</div>
      <div style="text-align:right;">Acciones</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="gl-rows-marker"></div>', unsafe_allow_html=True)
    for t in filtered:
        cat = t.get("category") or "término"
        badge_cls = _CAT_BADGE.get(cat, "b-term")
        note = (t.get("note") or "").strip()
        note_short = (note[:80] + "…") if len(note) > 80 else note

        c_src, c_tgt, c_cat, c_note, c_e, c_d = st.columns(
            [1.1, 1.1, 0.9, 1.7, 0.35, 0.35],
            gap="small",
            vertical_alignment="center",
        )
        c_src.markdown(
            f'<div class="gl-source">{escape(t.get("source",""))}</div>',
            unsafe_allow_html=True,
        )
        c_tgt.markdown(
            f'<div class="gl-target">{escape(t.get("target",""))}</div>',
            unsafe_allow_html=True,
        )
        c_cat.markdown(
            f'<span class="gl-badge {badge_cls}">{escape(cat)}</span>',
            unsafe_allow_html=True,
        )
        c_note.markdown(
            f'<div class="gl-note" title="{escape(note)}">'
            f'{escape(note_short) if note_short else "—"}</div>',
            unsafe_allow_html=True,
        )
        if c_e.button("✏️", key=f"gl_edit_{t['id']}",
                      help="Editar término", use_container_width=True):
            _dialog_edit_term(t)
        if c_d.button("🗑️", key=f"gl_del_{t['id']}",
                      help="Borrar término", use_container_width=True):
            if api_delete_glossary(t["id"]):
                api_get_glossary.clear()
                st.toast("Término borrado.", icon="🗑")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · HISTORIAL
# ═══════════════════════════════════════════════════════════════════════════
@st.dialog("Borrar todo el historial")
def _dialog_confirm_clear_history(jobs: list[dict]):
    """Confirmación antes de la acción más destructiva de la app: borra
    TODOS los proyectos persistidos (era un clic sin vuelta atrás)."""
    st.markdown(
        f"Vas a borrar **{len(jobs)} proyecto(s)** de forma permanente, "
        "incluidas sus traducciones guardadas. Esta acción no se puede "
        "deshacer."
    )
    col_no, col_si = st.columns(2)
    with col_no:
        if st.button("Cancelar", use_container_width=True):
            st.rerun()
    with col_si:
        if st.button("Sí, borrar todo", type="primary", use_container_width=True):
            # Los jobs vivos NO se borran (el backend además lo rechaza
            # con 409): borrar la fila de una película en curso dejaría
            # al worker gastando OpenAI sobre un job inexistente.
            activos = 0
            for j in jobs:
                if j.get("status") in ("queued", "running"):
                    activos += 1
                    continue
                api_delete_job(j["id"])
            api_get_jobs.clear()
            if activos:
                st.toast(
                    f"Historial vaciado ({activos} en curso conservados).",
                    icon="🗑",
                )
            else:
                st.toast("Historial vaciado.", icon="🗑")
            st.rerun()


def render_historial():
    # ── Lectura desde la API en cada render (fuente de verdad: SQLite) ──
    jobs = api_get_jobs()
    n     = len(jobs)
    n_ok  = sum(1 for j in jobs if j.get("status") == "completed")
    n_err = sum(1 for j in jobs if j.get("status") == "failed")
    n_act = sum(1 for j in jobs if j.get("status") in ("queued", "running"))

    resumen = f"{n} proyectos · {n_ok} completados · {n_err} con error"
    if n_act:
        resumen += f" · {n_act} en curso/cola"
    page_header(
        "Historial de Proyectos",
        resumen
        if n else "Las traducciones completadas aparecerán aquí automáticamente.",
    )

    if n == 0:
        empty_state("⏱", "Sin proyectos todavía",
                    "Las traducciones completadas aparecerán aquí automáticamente.")
        return

    # ── Mapeo de los campos del API a las columnas que mostramos ────────
    rows = []
    for j in jobs:
        # Fecha legible
        try:
            dt = datetime.fromisoformat(j["started_at"])
            fecha = dt.strftime("%d %b %Y, %H:%M")
        except (ValueError, KeyError):
            fecha = j.get("started_at", "—")

        # Duración formateada (segundos → "1m 23s" o "12.3s")
        s = j.get("elapsed_s", 0.0)
        duracion = f"{int(s)//60}m {int(s)%60:02d}s" if s >= 60 else f"{s:.1f}s"

        # Estado legible con icono (desde v3.8 el Historial también ve
        # jobs vivos: la cola es persistente en el backend)
        estado = j.get("status")
        status_label = {
            "completed": "✓ Completado",
            "failed":    "✕ Error",
            "queued":    "⏳ En cola",
            "running":   "🔄 En curso",
            "cancelled": "✖ Cancelado",
        }.get(estado, estado or "—")

        coste = _job_cost_eur(j.get("tokens_prompt"), j.get("tokens_completion"))

        # Métricas solo cuando existen: un job en cola con "% CPL 0.0"
        # y "0.0s" parece un dato real, no una ausencia.
        terminado = estado == "completed"

        # Sin scroll horizontal: columnas compactas y anchos explícitos.
        # "CPL" (config casi constante) queda fuera; "Idiomas" se mantiene
        # porque la app soporta varios destinos (es, es-419, fr, de, pt, it).
        rows.append({
            "ID":       f"JOB-{j['id']:05d}",
            "Archivo":  j.get("filename", "—"),
            "Idiomas":  f"→ {j.get('target_lang', 'es').upper()}",
            "Líneas":   j.get("n_translations", 0) or None,
            "% CPL":    j.get("cpl_compliance", 0.0) if terminado else None,
            "Coste":    coste,
            "Duración": duracion if terminado else None,
            "Fecha":    fecha,
            "Estado":   status_label,
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        height=min(58 + n * 38, 600),
        column_config={
            "ID":       st.column_config.TextColumn("ID",      width="small"),
            "Archivo":  st.column_config.TextColumn("Archivo", width="medium"),
            "Idiomas":  st.column_config.TextColumn("Idiomas", width="small"),
            "Líneas":   st.column_config.NumberColumn("Líneas", format="%d", width="small"),
            "% CPL":    st.column_config.NumberColumn("% CPL", format="%.1f%%", width="small"),
            "Coste":    st.column_config.NumberColumn(
                "Coste", format="%.3f €", width="small",
                help="Tokens reales que reportó OpenAI para este trabajo × "
                     "tarifa oficial de gpt-4o. Verificable contra "
                     "platform.openai.com/usage.",
            ),
            "Duración": st.column_config.TextColumn("Duración", width="small"),
            "Fecha":    st.column_config.TextColumn("Fecha",    width="medium"),
            "Estado":   st.column_config.TextColumn("Estado",   width="small"),
        },
    )

    # Total acumulado: el "recibo" del historial completo.
    total_eur = sum(c for c in (r["Coste"] for r in rows) if c is not None)
    st.markdown(
        f'<div style="font-size:12.5px;color:var(--text-3);margin-top:6px;'
        f'text-align:right;">Coste API total del historial: '
        f'<b>{total_eur:.2f} €</b></div>',
        unsafe_allow_html=True,
    )

    # ── Re-descarga del SRT de un proyecto ──────────────────────────────
    completed = [j for j in jobs if j.get("status") == "completed"]
    if completed:
        st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
        section_label("Descargar resultado")
        sel_col, dl_col = st.columns([3, 1.2], gap="small")
        with sel_col:
            opciones = {f"JOB-{j['id']:05d} · {j.get('filename', '—')}": j["id"]
                        for j in completed}
            elegido = st.selectbox(
                "proyecto", list(opciones), label_visibility="collapsed",
                key="hist_dl_select",
            )
        with dl_col:
            job_id = opciones[elegido]
            srt_bytes = _fetch_job_srt(job_id)
            if srt_bytes is not None:
                st.download_button(
                    "⬇ Descargar .srt",
                    data=srt_bytes,
                    file_name=f"traducido_job_{job_id}.srt",
                    mime="text/plain",
                    use_container_width=True,
                    key=f"hist_dl_{job_id}",
                )
            else:
                st.button(
                    "No disponible", disabled=True, use_container_width=True,
                    help="Proyecto anterior al archivado de resultados: el "
                         "SRT solo se conservó en la descarga original.",
                )

    st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
    if st.button("Limpiar historial", type="secondary", use_container_width=True):
        _dialog_confirm_clear_history(jobs)

# ═══════════════════════════════════════════════════════════════════════════
# OCR EN SEGUNDO PLANO (detección de texto sin bloquear la UI)
# ═══════════════════════════════════════════════════════════════════════════
# Mismo patrón que la cola de traducción: el POST al backend vive en un
# thread y el estado en cache_resource. Antes, la llamada era síncrona en
# el hilo de render: cualquier interacción (mover un slider) relanzaba el
# script y abortaba la espera — el backend seguía trabajando pero el
# resultado se perdía y la UI parecía "colgada".

@st.cache_resource
def _get_ocr_state() -> dict:
    return {
        "running_uuid": None,   # uuid del OCR en curso (None = libre)
        "video_name":   "",
        "result":       None,   # lista de detecciones al terminar
        "cancelled":    False,
        "error":        None,
        "started_at":   0.0,
    }


def _ocr_worker(state: dict, video_name: str, video_bytes: bytes,
                interval_s: float, min_conf: float, translate_on: bool,
                ocr_uuid: str) -> None:
    """Ejecuta la detección contra el backend. Vive en un thread daemon."""
    import base64 as _b64
    try:
        r = requests.post(
            f"{BACKEND_URL}/ocr/detect",
            files={"file": (video_name, video_bytes, "application/octet-stream")},
            data={
                "interval_s":     interval_s,
                "min_confidence": min_conf,
                "translate":      str(translate_on).lower(),
                "job_uuid":       ocr_uuid,
            },
            timeout=(10, 3600),
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("cancelled"):
            state["cancelled"] = True
            state["result"] = None
        else:
            detections = []
            for d in payload.get("detections", []):
                thumb = d.pop("thumbnail_b64", "")
                try:
                    d["thumbnail"] = _b64.b64decode(thumb) if thumb else b""
                except Exception:
                    d["thumbnail"] = b""
                detections.append(d)
            state["result"] = detections
    except requests.exceptions.RequestException as e:
        state["error"] = str(e)
    finally:
        state["running_uuid"] = None


def _render_ocr_progress(state: dict) -> None:
    """Barra de progreso real del OCR en curso + botón cancelar.

    El backend publica el avance frame a frame en job_logs; aquí se lee
    por polling (autorefresh) y se convierte en barra.
    """
    import re as _re

    uuid_ = state["running_uuid"]
    st_autorefresh(interval=2000, key="ocr_autorefresh")

    logs = _fetch_job_logs(uuid_, since=0)
    frame_done, frame_total = 0, 0
    last_msg = "Enviando vídeo al motor…"
    for entry in logs:
        msg = entry.get("message", "")
        m = _re.search(r"OCR frame (\d+)/(\d+)", msg)
        if m:
            frame_done, frame_total = int(m.group(1)), int(m.group(2))
        last_msg = msg

    bar_col, cancel_col = st.columns([5, 1], gap="small")
    with bar_col:
        if frame_total:
            st.progress(
                min(0.99, frame_done / frame_total),
                text=f"Analizando fotograma {frame_done}/{frame_total} · "
                     f"{escape(state['video_name'])}",
            )
        else:
            st.progress(0.02, text=f"{last_msg}")
    with cancel_col:
        if st.button("✖ Cancelar", key="ocr_cancel_btn",
                     use_container_width=True, type="secondary"):
            try:
                requests.post(f"{BACKEND_URL}/ocr/cancel/{uuid_}", timeout=5)
                st.toast("Cancelando… se detendrá al terminar el fotograma "
                         "actual.", icon="✖")
            except requests.exceptions.RequestException:
                st.toast("No se pudo contactar con el backend.", icon="⚠️")


# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · PREVIEW (QA visual del .srt traducido sobre el vídeo)
# ═══════════════════════════════════════════════════════════════════════════
@st.dialog("Editar cue")
def _dialog_edit_cue(cue: dict):
    """Modal para editar el texto de un cue (original o añadido).

    Originales: persiste en st.session_state.prv_edits (cue_idx → texto).
    Añadidos:   muta directamente el dict dentro de prv_added.
    """
    is_added = cue.get("is_added", False)
    label = ("Cue añadido"
             if is_added
             else f'#{cue["index"]}')
    st.markdown(
        f'<div class="mono" style="color:var(--text-3);font-size:12px;">'
        f'{label}  ·  {_format_timestamp(cue["start_s"])} → '
        f'{_format_timestamp(cue["end_s"])}'
        f'</div>',
        unsafe_allow_html=True,
    )

    if is_added:
        # Para añadidos, el texto actual está dentro de prv_added
        current = cue["text"]
        form_key = f"edit_cue_form_added_{cue['id']}"
    else:
        current = st.session_state.get("prv_edits", {}).get(
            cue["index"], cue["text"]
        )
        form_key = f"edit_cue_form_{cue['index']}"

    with st.form(form_key, clear_on_submit=False):
        new_text = st.text_area(
            "Texto del cue", value=current, height=120,
            help="Edita el texto que verá el espectador.",
        )
        c1, c2 = st.columns(2)
        cancel = c1.form_submit_button(
            "Cancelar", type="secondary", use_container_width=True,
        )
        save = c2.form_submit_button(
            "Guardar", use_container_width=True,
        )

    if cancel:
        st.rerun()
    if save:
        new_text_clean = new_text.strip()
        if not new_text_clean:
            st.error("El texto no puede estar vacío.")
            return

        if is_added:
            # Localizar y mutar el cue añadido por id
            for a in st.session_state.prv_added:
                if a["id"] == cue["id"]:
                    a["text"] = new_text_clean
                    break
            st.toast("Cue añadido actualizado.", icon="✅")
        else:
            st.session_state.setdefault("prv_edits", {})
            if new_text_clean == cue["text"].strip():
                st.session_state.prv_edits.pop(cue["index"], None)
                st.toast(f"Cue #{cue['index']} sin cambios.")
            else:
                st.session_state.prv_edits[cue["index"]] = new_text_clean
                st.toast(f"Cue #{cue['index']} actualizado.", icon="✅")
        st.rerun()


def _parse_user_timestamp(s: str) -> float | None:
    """Acepta 'HH:MM:SS,mmm', 'HH:MM:SS.mmm', 'MM:SS', '90.5' (segundos).
    Devuelve segundos como float, o None si no se puede parsear.
    """
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 3:
                h, m, sec = parts
                return int(h) * 3600 + int(m) * 60 + float(sec)
            if len(parts) == 2:
                m, sec = parts
                return int(m) * 60 + float(sec)
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


@st.dialog("Añadir nuevo cue")
def _dialog_add_cue(
    cues_existing: list[dict],
    default_start_s: float | None = None,
    default_end_s: float | None = None,
):
    """Modal para crear un cue nuevo. Persiste en st.session_state.prv_added
    como dict {id, start_s, end_s, text}. El SRT exportado fusiona estos
    cues con los originales ordenados por start_s.

    Si se pasan `default_start_s` / `default_end_s` (clic en + de un cue
    concreto) se usan como valores iniciales; si no, se sugiere arrancar
    justo tras el último cue existente.
    """
    st.markdown(
        '<div style="font-size:12.5px;color:var(--text-3);margin-bottom:14px;">'
        'Inserta un cue nuevo. Útil para letreros, texto en pantalla, '
        'o cuando el OCR no detecte algo y haya que añadirlo a mano.'
        '</div>',
        unsafe_allow_html=True,
    )

    if default_start_s is not None and default_end_s is not None:
        default_start = default_start_s
        default_end   = default_end_s
    else:
        last_end = max((c["end_s"] for c in cues_existing), default=0.0)
        default_start = last_end + 0.1
        default_end   = default_start + 2.0

    with st.form("add_cue_form", clear_on_submit=False):
        col_s, col_e = st.columns(2)
        with col_s:
            start_str = st.text_input(
                "Inicio (HH:MM:SS o segundos)",
                value=_format_srt_timestamp(default_start),
                help="Formatos aceptados: 00:01:23,500 · 01:23 · 90.5",
            )
        with col_e:
            end_str = st.text_input(
                "Fin (HH:MM:SS o segundos)",
                value=_format_srt_timestamp(default_end),
            )
        # Si venimos de "Añadir como cue" desde la detección OCR, el texto
        # traducido va pre-rellenado. Limpiamos el preset al cerrarse el
        # dialog (st.rerun() abajo) para que el siguiente Add no lo herede.
        preset = st.session_state.pop("prv_cv_preset_text", "")
        text = st.text_area(
            "Texto del cue",
            value=preset,
            placeholder="Ej: Lunes, 14 de marzo",
            height=100,
        )
        col_c, col_a = st.columns(2)
        cancel = col_c.form_submit_button(
            "Cancelar", type="secondary", use_container_width=True,
        )
        add = col_a.form_submit_button(
            "Añadir cue", use_container_width=True,
        )

    if cancel:
        st.rerun()
    if add:
        start_s = _parse_user_timestamp(start_str)
        end_s   = _parse_user_timestamp(end_str)
        if start_s is None or end_s is None:
            st.error("Formato de tiempo inválido. Usa HH:MM:SS,mmm o segundos.")
            return
        if end_s <= start_s:
            st.error("El fin debe ser mayor que el inicio.")
            return
        if not text.strip():
            st.error("El texto no puede estar vacío.")
            return

        import uuid as _uuid
        new_cue = {
            "id":      _uuid.uuid4().hex[:8],
            "start_s": start_s,
            "end_s":   end_s,
            "text":    text.strip(),
        }
        st.session_state.prv_added.append(new_cue)
        st.toast(
            f"Cue añadido en {_format_timestamp(start_s)}", icon="✅",
        )
        st.rerun()


def render_preview():
    # Estado inicial del editor (idempotente)
    st.session_state.setdefault("prv_edits", {})
    st.session_state.setdefault("prv_deleted", set())
    st.session_state.setdefault("prv_added", [])
    st.session_state.setdefault("prv_search", "")

    page_header(
        "Preview de traducción",
        "Carga el vídeo y el .srt traducido para previsualizar el resultado "
        "tal como lo verá el espectador. Sirve para QA antes de entregar al cliente.",
    )

    # ── Subida de archivos ─────────────────────────────────────────────
    col_video, col_srt = st.columns(2, gap="medium")
    with col_video:
        section_label("Vídeo")
        video_file = st.file_uploader(
            "video",
            type=["mp4", "webm", "mov", "mkv"],
            label_visibility="collapsed",
            key="prv_video",
        )
        if video_file is not None:
            sz = video_file.size
            sz_str = f"{sz/1024:.0f} KB" if sz < 1_048_576 else f"{sz/1_048_576:.1f} MB"
            st.markdown(
                f'<div style="font-size:11.5px;color:var(--text-4);margin-top:6px;">'
                f'{escape(video_file.name)} · {sz_str}</div>',
                unsafe_allow_html=True,
            )

    with col_srt:
        section_label("Subtítulos (.srt)")
        srt_file = st.file_uploader(
            "srt",
            type=["srt"],
            label_visibility="collapsed",
            key="prv_srt",
        )
        if srt_file is not None:
            cues_count = len(_parse_srt_bytes(srt_file.getvalue()))
            st.markdown(
                f'<div style="font-size:11.5px;color:var(--text-4);margin-top:6px;">'
                f'{escape(srt_file.name)} · {cues_count} cues</div>',
                unsafe_allow_html=True,
            )

    # ── Estados vacíos ─────────────────────────────────────────────────
    if video_file is None and srt_file is None:
        st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
        empty_state(
            "🎬",
            "Sube vídeo y subtítulos para empezar",
            "Acepta .mp4, .webm, .mov, .mkv para vídeo y .srt para subtítulos.",
        )
        return
    if video_file is None:
        st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
        empty_state("🎬", "Falta el vídeo", "Sube el .mp4 correspondiente al .srt.")
        return
    if srt_file is None:
        st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
        empty_state("📝", "Falta el .srt", "Sube el .srt traducido que quieres revisar.")
        return

    # ── Invalidar el estado del editor al cambiar de .srt ──────────────
    # Sin esto, las ediciones/borrados del archivo anterior se aplican por
    # índice al nuevo → "Descargar .srt corregido" produce un archivo
    # corrupto en silencio. Mismo patrón de firma que usa el bloque CV
    # para el vídeo (prv_cv_video_sig).
    srt_sig = (srt_file.name, srt_file.size)
    if st.session_state.get("prv_srt_sig") != srt_sig:
        st.session_state.prv_edits = {}
        st.session_state.prv_deleted = set()
        st.session_state.prv_added = []
        st.session_state.prv_srt_sig = srt_sig

    # ── Reproductor con subtítulos quemados ────────────────────────────
    srt_bytes_original = srt_file.getvalue()
    cues = _parse_srt_bytes(srt_bytes_original)
    edits:   dict[int, str] = st.session_state.prv_edits
    deleted: set[int]       = st.session_state.prv_deleted
    added:   list[dict]     = st.session_state.prv_added

    # Si hay cualquier cambio, el reproductor muestra el .srt CORREGIDO.
    has_changes = bool(edits) or bool(deleted) or bool(added)
    if has_changes:
        srt_bytes_effective = _build_modified_srt(cues, edits, deleted, added)
    else:
        srt_bytes_effective = srt_bytes_original

    st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)
    section_label(
        "Reproductor",
        right=f'<span class="mono" style="color:var(--text-4);font-size:12px;">'
              f'inicio: {_format_timestamp(st.session_state.get("prv_start_time", 0))}'
              f'</span>',
    )

    # start_time se actualiza cuando el usuario hace clic en un cue.
    # Esto fuerza un re-render del reproductor (Streamlit no expone seek
    # programático, solo el parámetro de inicio).
    st.video(
        video_file,
        subtitles=_srt_to_vtt(srt_bytes_effective),
        start_time=int(st.session_state.get("prv_start_time", 0)),
    )

    # ── Banner de cambios + botón de descarga ──────────────────────────
    if has_changes:
        st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
        col_msg, col_dl, col_reset = st.columns([3, 1.2, 0.6], gap="small")

        partes = []
        if edits:
            partes.append(f"{len(edits)} editado(s)")
        if deleted:
            partes.append(f"{len(deleted)} eliminado(s)")
        if added:
            partes.append(f"{len(added)} añadido(s)")
        resumen = " · ".join(partes)

        with col_msg:
            banner(
                "ok",
                f"Cambios pendientes: {resumen}",
                body="Los cambios se aplican en directo al reproductor. "
                     "Descarga el .srt corregido cuando termines.",
            )
        with col_dl:
            st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
            base_name = srt_file.name.rsplit(".", 1)[0]
            st.download_button(
                "⬇ Descargar .srt corregido",
                data=srt_bytes_effective,
                file_name=f"{base_name}_corregido.srt",
                mime="text/plain",
                use_container_width=True,
                key="prv_download_corrected",
            )
        with col_reset:
            st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
            if st.button("↺", type="secondary",
                         help="Descartar todos los cambios (editados, eliminados, añadidos)",
                         use_container_width=True, key="prv_reset_all"):
                st.session_state.prv_edits = {}
                st.session_state.prv_deleted = set()
                st.session_state.prv_added = []
                st.toast("Cambios descartados.")
                st.rerun()

    # ── Precomputar métricas técnicas (sobre texto efectivo) ───────────
    # Ignora cues eliminados. Mete los añadidos en orden cronológico
    # para que el gap se calcule respecto al verdadero "cue siguiente".
    live_effective: list[dict] = []
    for c in cues:
        if c["index"] in deleted:
            continue
        live_effective.append({
            "index":     c["index"],
            "id":        None,                                 # marca: original
            "start_s":   c["start_s"],
            "end_s":     c["end_s"],
            "text":      edits.get(c["index"], c["text"]),
            "is_added":  False,
        })
    for a in added:
        live_effective.append({
            "index":     None,                                 # los añadidos no tienen idx original
            "id":        a["id"],
            "start_s":   a["start_s"],
            "end_s":     a["end_s"],
            "text":      a["text"],
            "is_added":  True,
        })
    live_effective.sort(key=lambda c: c["start_s"])

    # Clave única para mapear métricas (idx para originales, id para añadidos)
    def _cue_key(c: dict) -> str:
        return f"a_{c['id']}" if c["is_added"] else f"o_{c['index']}"

    metrics_by_key: dict[str, dict] = {}
    for i, c in enumerate(live_effective):
        next_c = live_effective[i + 1] if i + 1 < len(live_effective) else None
        metrics_by_key[_cue_key(c)] = _compute_cue_metrics(c, next_c)

    # ── Stats globales: 4 tarjetas con compliance del .srt activo ───────
    n_live = len(live_effective)
    if n_live > 0:
        n_problems = sum(1 for m in metrics_by_key.values() if m["status"] != "ok")
        n_cpl_ok   = sum(1 for m in metrics_by_key.values() if m["cpl_max"] <= DEFAULT_CPL)
        n_cps_ok   = sum(1 for m in metrics_by_key.values() if m["cps"] <= CPS_LIMIT)
        pct_cpl = round(n_cpl_ok * 100 / n_live, 1)
        pct_cps = round(n_cps_ok * 100 / n_live, 1)

        st.markdown('<div style="height:18px;"></div>', unsafe_allow_html=True)
        st.markdown(f"""
        <div class="gl-stats">
          <div class="gl-stat"><div class="v">{n_live}</div><div class="l">Cues activos</div></div>
          <div class="gl-stat"><div class="v">{pct_cpl}%</div><div class="l">CPL ≤ {DEFAULT_CPL} (UNE 153010)</div></div>
          <div class="gl-stat"><div class="v">{pct_cps}%</div><div class="l">CPS ≤ {CPS_LIMIT:.0f}</div></div>
          <div class="gl-stat"><div class="v">{n_problems}</div><div class="l">Con problemas</div></div>
        </div>
        """, unsafe_allow_html=True)

    # ── Buscador + filtro + botón añadir + contador ────────────────────
    search_col, filter_col, add_col, label_col = st.columns(
        [2, 1.3, 0.7, 0.8], gap="medium"
    )
    with search_col:
        search_query = st.text_input(
            "Buscar",
            value=st.session_state.prv_search,
            placeholder="🔍   Buscar texto en los cues…",
            label_visibility="collapsed",
            key="prv_search_input",
        )
        st.session_state.prv_search = search_query

    FILTER_OPTIONS = [
        "Todos los cues",
        "Con problemas",
        "Con problemas críticos",
        "Solo CPL excedido",
        "Solo CPS excedido",
        "Solo duración fuera de rango",
    ]
    with filter_col:
        filter_choice = st.selectbox(
            "Filtro",
            FILTER_OPTIONS,
            label_visibility="collapsed",
            key="prv_filter",
        )
    with add_col:
        if st.button("➕ Añadir cue", use_container_width=True,
                     help="Insertar un cue nuevo en cualquier timestamp",
                     key="prv_add_cue_btn"):
            _dialog_add_cue(live_effective)

    # ── Construir la lista de cues a mostrar (originales + añadidos) ───
    # Originales pueden estar eliminados o no; los añadidos siempre vivos.
    view_cues: list[dict] = []
    for c in cues:
        if c["index"] in deleted:
            view_cues.append({
                "index":     c["index"],
                "id":        None,
                "start_s":   c["start_s"],
                "end_s":     c["end_s"],
                "text":      c["text"],
                "is_added":  False,
                "is_deleted": True,
            })
        else:
            view_cues.append({
                "index":     c["index"],
                "id":        None,
                "start_s":   c["start_s"],
                "end_s":     c["end_s"],
                "text":      edits.get(c["index"], c["text"]),
                "is_added":  False,
                "is_deleted": False,
            })
    for a in added:
        view_cues.append({
            "index":     None,
            "id":        a["id"],
            "start_s":   a["start_s"],
            "end_s":     a["end_s"],
            "text":      a["text"],
            "is_added":  True,
            "is_deleted": False,
        })
    view_cues.sort(key=lambda c: c["start_s"])

    # Aplicar búsqueda
    q = (search_query or "").strip().lower()
    if q:
        view_cues = [c for c in view_cues if q in c["text"].lower()]

    # Aplicar filtro por tipo de problema
    def _passes_filter(c: dict) -> bool:
        if c["is_deleted"]:
            return filter_choice == "Todos los cues"
        m = metrics_by_key.get(_cue_key(c))
        if m is None:
            return True
        if filter_choice == "Todos los cues":          return True
        if filter_choice == "Con problemas":           return m["status"] != "ok"
        if filter_choice == "Con problemas críticos":  return m["status"] == "err"
        if filter_choice == "Solo CPL excedido":       return m["cpl_max"] > DEFAULT_CPL
        if filter_choice == "Solo CPS excedido":       return m["cps"] > 17
        if filter_choice == "Solo duración fuera de rango":
            return m["duration"] < 0.833 or m["duration"] > 7.0
        return True

    if filter_choice != "Todos los cues":
        view_cues = [c for c in view_cues if _passes_filter(c)]

    with label_col:
        n_filt = len(view_cues)
        n_total = len(cues) + len(added)
        any_filter = bool(q) or filter_choice != "Todos los cues"
        info = f'{n_filt} de {n_total}' if any_filter else f'{n_total} cues'
        if edits:   info += f' · {len(edits)} ed'
        if deleted: info += f' · {len(deleted)} elim'
        if added:   info += f' · {len(added)} añ'
        st.markdown(
            f'<div class="sl-row" style="margin-top:6px;justify-content:flex-end;">'
            f'<span class="mono" style="color:var(--text-4);font-size:11.5px;">{info}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # Para compatibilidad con el bucle de abajo, renombro
    filtered = view_cues

    if not cues:
        empty_state("📝", "El .srt no tiene cues válidos",
                    "Comprueba el formato del archivo.")
        return

    if not filtered:
        empty_state("🔎", "Sin resultados",
                    "Cambia o borra la búsqueda.")
        return

    # ── Paginación: solo si la lista visible > CUES_PER_PAGE ───────────
    CUES_PER_PAGE = 50
    st.session_state.setdefault("prv_cues_page", 0)
    total_cue_pages = max(1, (len(filtered) + CUES_PER_PAGE - 1) // CUES_PER_PAGE)
    # min() evita que un filtro restrictivo deje la página actual fuera de rango
    cue_page = min(st.session_state.prv_cues_page, total_cue_pages - 1)

    if total_cue_pages > 1:
        p_prev, p_info, p_next = st.columns([1, 2, 1], gap="small")
        with p_prev:
            if st.button("◀ Anterior", use_container_width=True,
                         key="prv_cues_page_prev",
                         disabled=cue_page == 0):
                st.session_state.prv_cues_page = max(0, cue_page - 1)
                st.rerun()
        with p_info:
            start_idx = cue_page * CUES_PER_PAGE + 1
            end_idx = min(start_idx + CUES_PER_PAGE - 1, len(filtered))
            st.markdown(
                f'<div class="mono" style="text-align:center;'
                f'color:var(--text-3);font-size:11.5px;margin-top:8px;">'
                f'Página {cue_page+1}/{total_cue_pages} · '
                f'Mostrando {start_idx}-{end_idx} de {len(filtered)}'
                f'</div>',
                unsafe_allow_html=True,
            )
        with p_next:
            if st.button("Siguiente ▶", use_container_width=True,
                         key="prv_cues_page_next",
                         disabled=cue_page >= total_cue_pages - 1):
                st.session_state.prv_cues_page = min(total_cue_pages - 1, cue_page + 1)
                st.rerun()

    # Header HTML de la tabla (7 columnas: #, ts, texto, ▶, ✏️, 🗑, +)
    st.markdown("""
    <div class="gl-thead-grid" style="grid-template-columns:0.5fr 0.7fr 2.4fr 0.35fr 0.35fr 0.35fr 0.35fr;">
      <div>#</div>
      <div>Timestamp</div>
      <div>Texto</div>
      <div style="text-align:right;">Ir</div>
      <div style="text-align:right;">Editar</div>
      <div style="text-align:right;">Borrar</div>
      <div style="text-align:right;">Añadir</div>
    </div>
    """, unsafe_allow_html=True)

    # Slice de la página actual (si total ≤ CUES_PER_PAGE, equivale a filtered entero)
    page_start = cue_page * CUES_PER_PAGE
    page_end = page_start + CUES_PER_PAGE
    cues_to_render = filtered[page_start:page_end]

    # Filas de cues
    st.markdown('<div class="gl-rows-marker"></div>', unsafe_allow_html=True)
    for cue in cues_to_render:
        c_idx, c_ts, c_txt, c_seek, c_edit, c_del, c_add = st.columns(
            [0.5, 0.7, 2.4, 0.35, 0.35, 0.35, 0.35],
            gap="small",
            vertical_alignment="center",
        )
        is_added   = cue["is_added"]
        is_deleted = cue["is_deleted"]
        is_edited  = (not is_added) and (cue["index"] in edits)
        ckey       = _cue_key(cue)

        # Estilo según estado del cue
        if is_deleted:
            idx_color = "var(--text-4)"
            text_color = "var(--text-4)"
            text_extra = "text-decoration:line-through;"
            idx_mark = "✕"
            idx_label = str(cue["index"])
        elif is_added:
            idx_color = "var(--info-fg)"
            text_color = "var(--info-fg-2)"
            text_extra = "font-weight:500;"
            idx_mark = ""
            idx_label = "✦"
        elif is_edited:
            idx_color = "var(--ok-fg)"
            text_color = "var(--ok-fg-2)"
            text_extra = "font-weight:500;"
            idx_mark = "*"
            idx_label = str(cue["index"])
        else:
            idx_color = "var(--text-3)"
            text_color = "var(--text)"
            text_extra = ""
            idx_mark = ""
            idx_label = str(cue["index"])

        # Dot de estado (verde/ámbar/rojo) + tooltip con issues
        if is_deleted:
            dot_html = ""
        else:
            m = metrics_by_key.get(ckey, {"status": "ok", "issues": []})
            dot_color = {
                "ok":   "var(--ok-fg)",
                "warn": "var(--warn-fg)",
                "err":  "var(--err-fg)",
            }.get(m["status"], "var(--text-4)")
            tooltip = " · ".join(m["issues"]) if m["issues"] else "OK"
            dot_html = (
                f'<span title="{escape(tooltip)}" '
                f'style="display:inline-block;width:8px;height:8px;'
                f'border-radius:50%;background:{dot_color};'
                f'margin-right:6px;vertical-align:middle;"></span>'
            )

        c_idx.markdown(
            f'<div class="mono" style="color:{idx_color};font-size:12px;font-weight:600;">'
            f'{dot_html}{idx_label}{idx_mark}</div>',
            unsafe_allow_html=True,
        )
        c_ts.markdown(
            f'<div class="mono" style="color:var(--text-2);font-size:12px;">'
            f'{_format_timestamp(cue["start_s"])}</div>',
            unsafe_allow_html=True,
        )
        # Texto del cue + mini-etiqueta con el motivo del estado (si lo hay)
        text_html = (
            f'<div style="font-size:12.8px;color:{text_color};{text_extra}">'
            f'{escape(cue["text"]).replace(chr(10), "<br>")}</div>'
        )
        # Sub-etiqueta con los issues (solo si no está borrado y tiene problemas)
        if not is_deleted:
            m = metrics_by_key.get(ckey)
            if m and m["status"] != "ok" and m["issues"]:
                badge_bg = {
                    "warn": "var(--warn-bg)",
                    "err":  "var(--err-bg)",
                }.get(m["status"], "var(--hover-bg)")
                badge_fg = {
                    "warn": "var(--warn-fg-2)",
                    "err":  "var(--err-fg-2)",
                }.get(m["status"], "var(--text-3)")
                # Mostrar los primeros 2 issues; si hay más, indicar +N
                shown = m["issues"][:2]
                extra = len(m["issues"]) - len(shown)
                issues_text = " · ".join(shown)
                if extra > 0:
                    issues_text += f" · +{extra}"
                text_html += (
                    f'<div style="display:inline-block;font-size:10.5px;'
                    f'font-weight:500;color:{badge_fg};background:{badge_bg};'
                    f'padding:2px 8px;border-radius:99px;margin-top:4px;">'
                    f'{escape(issues_text)}</div>'
                )
        c_txt.markdown(text_html, unsafe_allow_html=True)
        if c_seek.button("▶", key=f"prv_seek_{ckey}",
                         help=f"Saltar a {_format_timestamp(cue['start_s'])}",
                         use_container_width=True,
                         disabled=is_deleted):
            st.session_state.prv_start_time = cue["start_s"]
            st.rerun()
        if c_edit.button("✏️", key=f"prv_edit_{ckey}",
                         help="Editar texto del cue",
                         use_container_width=True,
                         disabled=is_deleted):
            _dialog_edit_cue(cue)

        # Botón borrar / restaurar (alterna según estado)
        if is_deleted:
            if c_del.button("↺", key=f"prv_undel_{ckey}",
                            help="Restaurar este cue",
                            use_container_width=True):
                st.session_state.prv_deleted.discard(cue["index"])
                st.toast(f"Cue #{cue['index']} restaurado.")
                st.rerun()
        elif is_added:
            if c_del.button("🗑", key=f"prv_del_{ckey}",
                            help="Quitar este cue añadido",
                            use_container_width=True):
                st.session_state.prv_added = [
                    a for a in st.session_state.prv_added
                    if a["id"] != cue["id"]
                ]
                st.toast("Cue añadido eliminado.", icon="🗑")
                st.rerun()
        else:
            if c_del.button("🗑", key=f"prv_del_{ckey}",
                            help="Eliminar este cue del .srt",
                            use_container_width=True):
                st.session_state.prv_deleted.add(cue["index"])
                st.session_state.prv_edits.pop(cue["index"], None)
                st.toast(f"Cue #{cue['index']} eliminado.", icon="🗑")
                st.rerun()

        # Botón + para insertar un cue nuevo justo después de éste
        if c_add.button("+", key=f"prv_add_after_{ckey}",
                        help="Insertar un cue nuevo a continuación "
                             "(timestamps editables en el diálogo)",
                        use_container_width=True,
                        disabled=is_deleted):
            _dialog_add_cue(
                live_effective,
                default_start_s=cue["end_s"] + 0.1,
                default_end_s=cue["end_s"] + 2.1,
            )

    # Controles de paginación al final también (para no tener que hacer
    # scroll arriba si la página tiene muchas filas).
    if total_cue_pages > 1:
        st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
        p_prev2, p_info2, p_next2 = st.columns([1, 2, 1], gap="small")
        with p_prev2:
            if st.button("◀ Anterior", use_container_width=True,
                         key="prv_cues_page_prev_bottom",
                         disabled=cue_page == 0):
                st.session_state.prv_cues_page = max(0, cue_page - 1)
                st.rerun()
        with p_info2:
            start_idx = cue_page * CUES_PER_PAGE + 1
            end_idx = min(start_idx + CUES_PER_PAGE - 1, len(filtered))
            st.markdown(
                f'<div class="mono" style="text-align:center;'
                f'color:var(--text-3);font-size:11.5px;margin-top:8px;">'
                f'Página {cue_page+1}/{total_cue_pages} · '
                f'{start_idx}-{end_idx} de {len(filtered)}'
                f'</div>',
                unsafe_allow_html=True,
            )
        with p_next2:
            if st.button("Siguiente ▶", use_container_width=True,
                         key="prv_cues_page_next_bottom",
                         disabled=cue_page >= total_cue_pages - 1):
                st.session_state.prv_cues_page = min(total_cue_pages - 1, cue_page + 1)
                st.rerun()

    # ── Sección Computer Vision: detección de texto en pantalla ────────
    st.markdown('<div style="height:32px;"></div>', unsafe_allow_html=True)
    section_label(
        "Detección de texto en pantalla (CV)",
        right='<span class="mono" style="color:var(--text-4);font-size:11.5px;">'
              'EasyOCR · v3.2 nivel 1</span>',
    )
    st.markdown(
        '<div style="font-size:12.5px;color:var(--text-3);'
        'margin-bottom:14px;line-height:1.6;">'
        'Analiza el vídeo frame a frame buscando regiones con texto en '
        'pantalla (letreros, carteles, mensajes, títulos). Útil para '
        'detectar contenido que NO está en el .srt y que el dialoguista '
        'tendría que añadir a mano. Todo el procesamiento se hace local '
        '— el vídeo no sale del servidor.'
        '</div>',
        unsafe_allow_html=True,
    )

    cfg_col1, cfg_col2, cfg_col3, btn_col = st.columns(
        [1.1, 1.1, 1.0, 0.9], gap="medium",
    )
    with cfg_col1:
        interval_s = st.slider(
            "Sample cada N segundos",
            min_value=0.5, max_value=10.0, value=3.0, step=0.5,
            key="prv_cv_interval",
            help="Menor = más cobertura, más tiempo. ~7-10s por frame a 1080p.",
        )
    with cfg_col2:
        min_conf = st.slider(
            "Confianza mínima",
            min_value=0.0, max_value=1.0, value=0.35, step=0.05,
            key="prv_cv_min_conf",
            help="Descarta detecciones con confianza menor. Los títulos con "
                 "tipografías estilizadas suelen salir con confianza 0.3-0.5; "
                 "si ves ruido, sube el umbral.",
        )
    with cfg_col3:
        st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
        translate_on = st.toggle(
            "Pre-traducir (gpt-4o-mini)",
            value=False,
            key="prv_cv_translate_on",
            help="Si activo, traduce los textos detectados a español con "
                 "gpt-4o-mini (coste ~$0.0001 por video). Si no, los "
                 "textos se muestran solo en inglés y se traducen a mano "
                 "al añadirlos como cue.",
        )
    with btn_col:
        st.markdown('<div style="height:26px;"></div>', unsafe_allow_html=True)
        _ocr_busy = _get_ocr_state()["running_uuid"] is not None
        detect_btn = st.button(
            "⏳ Detectando…" if _ocr_busy else "🔍 Detectar texto",
            use_container_width=True,
            key="prv_cv_detect_btn",
            type="secondary",
            disabled=_ocr_busy,
        )

    # Si el usuario sube un vídeo distinto, invalidar detecciones previas
    current_video_sig = (video_file.name, video_file.size)
    if st.session_state.get("prv_cv_video_sig") != current_video_sig:
        st.session_state.prv_cv_detections = None
        st.session_state.prv_cv_page = 0
        st.session_state.prv_cv_video_sig = current_video_sig

    # ── OCR asíncrono: lanzar / progreso / recoger resultado ───────────
    ocr_state = _get_ocr_state()

    if detect_btn and ocr_state["running_uuid"] is None:
        import uuid as _uuid
        ocr_uuid = str(_uuid.uuid4())
        # Reset del estado y lanzamiento del worker. Los bytes del vídeo
        # se leen AQUÍ (hilo principal): el thread no puede tocar el
        # file_uploader de Streamlit.
        ocr_state.update({
            "running_uuid": ocr_uuid,
            "video_name":   video_file.name,
            "result":       None,
            "cancelled":    False,
            "error":        None,
            "started_at":   time.time(),
        })
        threading.Thread(
            target=_ocr_worker,
            args=(ocr_state, video_file.name, video_file.getvalue(),
                  interval_s, min_conf, translate_on, ocr_uuid),
            daemon=True,
        ).start()
        st.rerun()
    elif detect_btn:
        st.toast("Ya hay una detección en curso — cancélala primero si "
                 "quieres relanzar con otros parámetros.", icon="⏳")

    if ocr_state["running_uuid"] is not None:
        # En curso: barra de progreso real + cancelar. Tocar sliders u
        # otras partes de la página ya NO afecta: el trabajo vive en su
        # propio thread y esta sección solo lo observa.
        _render_ocr_progress(ocr_state)
    elif ocr_state["result"] is not None:
        # Terminado: recoger el resultado y limpiarlo del estado compartido
        detections = ocr_state["result"]
        ocr_state["result"] = None
        dt = time.time() - ocr_state["started_at"]
        st.toast(f"OCR: {len(detections)} textos en {dt:.0f}s", icon="🔍")
        st.session_state.prv_cv_detections = detections
        st.session_state.prv_cv_page = 0
        st.rerun()
    elif ocr_state["cancelled"]:
        ocr_state["cancelled"] = False
        st.toast("Detección cancelada.", icon="✖")
    elif ocr_state["error"]:
        st.error(f"La detección falló: {ocr_state['error']}")
        ocr_state["error"] = None

    # Mostrar resultados si los hay
    detections = st.session_state.get("prv_cv_detections")
    if detections is not None:
        st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)

        if not detections:
            empty_state(
                "🔎",
                "No se detectó texto en el vídeo",
                "Prueba con un intervalo más pequeño, baja la confianza, "
                "o este vídeo no tiene texto en pantalla.",
            )
        else:
            # Paginación: 12 detecciones por página (4 filas x 3 columnas)
            DETECTIONS_PER_PAGE = 12
            total_pages = (len(detections) + DETECTIONS_PER_PAGE - 1) // DETECTIONS_PER_PAGE
            st.session_state.setdefault("prv_cv_page", 0)
            current_page = min(st.session_state.prv_cv_page, total_pages - 1)

            section_label(
                f"{len(detections)} detecciones",
                right=(
                    f'<span class="mono" style="color:var(--text-4);'
                    f'font-size:11.5px;">página {current_page+1}/{total_pages}'
                    f' · click ➕ para añadir como cue</span>'
                ),
            )

            # Controles de paginación
            if total_pages > 1:
                pag_prev, pag_info, pag_next = st.columns([1, 2, 1], gap="small")
                with pag_prev:
                    if st.button("◀ Anterior", use_container_width=True,
                                 key="prv_cv_page_prev",
                                 disabled=current_page == 0):
                        st.session_state.prv_cv_page = max(0, current_page - 1)
                        st.rerun()
                with pag_info:
                    start_idx = current_page * DETECTIONS_PER_PAGE + 1
                    end_idx = min(start_idx + DETECTIONS_PER_PAGE - 1, len(detections))
                    st.markdown(
                        f'<div class="mono" style="text-align:center;'
                        f'color:var(--text-3);font-size:12px;'
                        f'margin-top:8px;">Mostrando {start_idx}-{end_idx} '
                        f'de {len(detections)}</div>',
                        unsafe_allow_html=True,
                    )
                with pag_next:
                    if st.button("Siguiente ▶", use_container_width=True,
                                 key="prv_cv_page_next",
                                 disabled=current_page >= total_pages - 1):
                        st.session_state.prv_cv_page = min(total_pages - 1, current_page + 1)
                        st.rerun()

            # Slice de la página actual
            page_start = current_page * DETECTIONS_PER_PAGE
            page_end = page_start + DETECTIONS_PER_PAGE
            page_items = detections[page_start:page_end]

            # Galería en filas de 3 columnas
            cols_per_row = 3
            for i in range(0, len(page_items), cols_per_row):
                row_cols = st.columns(cols_per_row, gap="medium")
                for j, d in enumerate(page_items[i:i + cols_per_row]):
                    with row_cols[j]:
                        if d.get("thumbnail"):
                            st.image(
                                d["thumbnail"],
                                caption=f"Texto detectado: {d.get('text', '')[:60]}",
                            )
                        else:
                            st.caption("(miniatura no disponible)")
                        # Color del badge según confianza
                        conf = d.get("confidence", 0.0)
                        if conf >= 0.7:
                            badge_color = "var(--ok-fg)"
                            badge_bg = "var(--ok-bg)"
                        elif conf >= 0.4:
                            badge_color = "var(--warn-fg)"
                            badge_bg = "var(--warn-bg)"
                        else:
                            badge_color = "var(--err-fg)"
                            badge_bg = "var(--err-bg)"

                        st.markdown(
                            f'<div style="display:flex;justify-content:space-between;'
                            f'align-items:center;margin-top:6px;">'
                            f'<span class="mono" style="color:var(--text-2);font-size:12px;">'
                            f'{_format_timestamp(d["timestamp_s"])}</span>'
                            f'<span style="background:{badge_bg};color:{badge_color};'
                            f'font-size:10.5px;font-weight:600;padding:2px 8px;'
                            f'border-radius:99px;">conf {conf:.2f}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        # Texto detectado en inglés
                        st.markdown(
                            f'<div style="font-size:11.5px;color:var(--text-3);'
                            f'margin-top:6px;font-family:var(--font-mono);'
                            f'background:var(--hover-bg);padding:5px 8px;'
                            f'border-radius:6px;word-break:break-word;">'
                            f'EN: {escape(d.get("text", ""))}</div>',
                            unsafe_allow_html=True,
                        )
                        # Texto traducido (si Nivel 3 lo generó)
                        tgt = d.get("text_translated", "")
                        if tgt:
                            st.markdown(
                                f'<div style="font-size:12px;color:var(--text);'
                                f'margin-top:4px;font-weight:500;'
                                f'background:var(--info-bg);padding:5px 8px;'
                                f'border-radius:6px;word-break:break-word;">'
                                f'ES: {escape(tgt)}</div>',
                                unsafe_allow_html=True,
                            )

                        if st.button(
                            "➕ Añadir como cue",
                            use_container_width=True,
                            key=f"prv_cv_add_{page_start + i + j}_"
                                f"{d['timestamp_s']:.2f}",
                        ):
                            # Pre-rellenamos el dialog con la traducción si existe
                            # Eso lo hace _dialog_add_cue por defecto si st.session_state
                            # tiene un "preset_text" — pero como no lo tiene, lo
                            # mejor es exponer este caso de uso vía un dialog
                            # específico. Por simplicidad, abrimos el dialog
                            # estándar con timestamp prellenado; el usuario
                            # copia el texto traducido manualmente.
                            st.session_state["prv_cv_preset_text"] = tgt or d.get("text", "")
                            _dialog_add_cue(
                                live_effective,
                                default_start_s=d["timestamp_s"],
                                default_end_s=d["timestamp_s"] + 2.5,
                            )


# ═══════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════
if   st.session_state.page == "workspace": render_workspace()
elif st.session_state.page == "glosario":  render_glosario()
elif st.session_state.page == "historial": render_historial()
elif st.session_state.page == "preview":   render_preview()
