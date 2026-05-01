"""
Subtitulam — Streamlit app (high-fidelity).
Replica el prototipo React en un único archivo usando CSS inyectado,
componentes HTML personalizados y st.session_state para el flujo SPA.

Ejecutar:  streamlit run app_ui.py
Backend:   uvicorn app.main:app --reload   (puerto 8000)
"""
from __future__ import annotations

import time
from datetime import datetime
from html import escape
from typing import Optional

import pandas as pd
import requests
import streamlit as st

BACKEND_URL = "http://localhost:8000"


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


@st.cache_data(show_spinner=False)
def api_get_jobs() -> list[dict]:
    """Lista los jobs (historial) más recientes primero.

    Cacheada sin TTL — invalidar con api_get_jobs.clear() tras
    POST /translate exitoso o DELETE /jobs.
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
_DEFAULTS: dict = {
    "page":            "workspace",
    "ws_state":        "idle",       # idle | translating | success | error
    "ws_progress":     0,
    "ws_result_bytes": None,
    "ws_result_name":  None,
    "ws_error":        None,
    "ws_metrics":      None,
    "context_global":  "",
    "cpl_limit":       42,
    "target_lang":     "es",
    "glossary":        [],
    "history":         [],
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
.bnr .ico{width:22px;height:22px;border-radius:99px;display:flex;align-items:center;justify-content:center;flex-shrink:0;color:#fff;font-size:12px;font-weight:700;margin-top:1px;}
.bnr.ok .ico{background:var(--ok-fg);} .bnr.err .ico{background:var(--err-fg);} .bnr.warn .ico{background:var(--warn-fg);}
.bnr .t{font-size:13px;font-weight:600;}
.bnr.ok .t{color:var(--ok-fg);} .bnr.err .t{color:var(--err-fg);} .bnr.warn .t{color:var(--warn-fg);}
.bnr .s{font-size:12.5px;margin-top:2px;}
.bnr.ok .s{color:var(--ok-fg-2);} .bnr.err .s{color:var(--err-fg-2);} .bnr.warn .s{color:var(--warn-fg-2);}
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

def status_pill(kind: str) -> str:
    label = {"ok": "Completado", "err": "Error", "warn": "Revisión",
             "run": "En curso", "queued": "En cola"}.get(kind, kind)
    return f'<span class="st-pill {kind}"><span class="dot"></span>{label}</span>'

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
# BACKEND HOOK
# ═══════════════════════════════════════════════════════════════════════════
def process_translation(srt_file, context: str, target_lang: str, cpl_limit: int) -> dict:
    """
    Envía el .srt al backend con contexto, idioma destino y límite CPL.
    Devuelve {bytes, filename, metrics} con métricas reales calculadas sobre
    el SRT traducido (líneas, CPL compliance, idiomas).
    """
    response = requests.post(
        f"{BACKEND_URL}/translate",
        files={"file": (srt_file.name, srt_file.getvalue(), "text/plain")},
        data={
            "context":     context,
            "target_lang": target_lang,
            "cpl":         cpl_limit,
        },
        timeout=300,
    )
    response.raise_for_status()

    # ── Métricas reales calculadas sobre el SRT devuelto ────────────────────
    translated_text = response.content.decode("utf-8", errors="ignore")
    lines = [ln for ln in translated_text.splitlines()
             if ln.strip() and "-->" not in ln and not ln.strip().isdigit()]
    n_lines = len(lines)
    n_over_cpl = sum(1 for ln in lines if len(ln) > cpl_limit)
    cpl_rate = f"{(n_lines - n_over_cpl) / max(1, n_lines) * 100:.1f}%"

    return {
        "bytes":    response.content,
        "filename": f"{target_lang}_{srt_file.name}",
        "metrics":  {
            "cpl_rate": cpl_rate,
            "lines":    str(n_lines),
            "langs":    f"EN → {target_lang.upper()}",
        },
    }

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

    st.markdown("""
    <div class="sb-foot">
      <div class="dim">GPT-4o · v1.5</div>
    </div>
    """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · WORKSPACE
# ═══════════════════════════════════════════════════════════════════════════
def render_workspace():
    trn_id = f"TRN-{(842 + len(st.session_state.history)):05d}"
    page_header(
        "Nueva Traducción",
        "Sube tu archivo .srt y tradúcelo con IA, respetando CPL, glosario y contexto narrativo.",
        right=f'<span class="st-pill queued mono"><span class="dot"></span>{trn_id}</span>',
    )

    # Contexto
    section_label("Contexto global")
    st.session_state.context_global = st.text_input(
        "ctx",
        value=st.session_state.context_global,
        placeholder='Ej: "Breaking Bad" — serie dramática. Walter White, protagonista. No adaptar nombres propios.',
        label_visibility="collapsed",
    )
    st.markdown('<div class="hint">Este texto se usara como contexto para mejorar la coherencia narrativa.</div>', unsafe_allow_html=True)

    # Archivos
    section_label("Archivos")
    col_srt, col_mp4 = st.columns(2, gap="medium")
    with col_srt:
        st.markdown('<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">Subtítulo .srt <span style="color:#ef4444;">*</span></div>', unsafe_allow_html=True)
        srt_file = st.file_uploader("srt", type=["srt"], label_visibility="collapsed", key="srt_up")
        if srt_file:
            file_pill(srt_file.name, srt_file.size)
    with col_mp4:
        st.markdown('<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">Vídeo de referencia .mp4 <em style="color:var(--text-4);font-style:normal;">(opcional)</em></div>', unsafe_allow_html=True)
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

    # CTA
    st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
    clicked = st.button(
        "Iniciar Traducción con IA  →",
        disabled=(srt_file is None or st.session_state.ws_state == "translating"),
        use_container_width=True,
    )

    # Flujo de traducción
    if clicked and srt_file:
        st.session_state.ws_state = "translating"
        st.session_state.ws_error = None
        st.session_state.ws_result_bytes = None

        stages = [
            (18, "Analizando contexto…"),
            (35, "Conectando con GPT-4o…"),
            (62, "Aplicando glosario RAG…"),
            (88, "Ajustando CPL…"),
        ]
        placeholder = st.empty()
        for pct, label in stages:
            with placeholder.container():
                progress_bar(pct, label)
            time.sleep(0.25)

        try:
            with placeholder.container():
                progress_bar(95, "Finalizando traducción…")
            result = process_translation(
                srt_file,
                st.session_state.context_global,
                st.session_state.target_lang,
                st.session_state.cpl_limit,
            )
            placeholder.empty()
            st.session_state.ws_state = "success"
            st.session_state.ws_result_bytes = result["bytes"]
            st.session_state.ws_result_name  = result["filename"]
            st.session_state.ws_metrics      = result["metrics"]
            # El backend ya persistió el job y sus translations en SQLite
            # vía save_completed_job() — invalidamos el cache para verlo.
            api_get_jobs.clear()
            st.rerun()
        except requests.exceptions.ConnectionError:
            placeholder.empty()
            st.session_state.ws_state = "error"
            st.session_state.ws_error = "No se puede conectar al backend. Asegúrate de que el servidor está activo."
        except Exception as exc:
            placeholder.empty()
            st.session_state.ws_state = "error"
            st.session_state.ws_error = str(exc)

    # Resultado
    if st.session_state.ws_state == "success" and st.session_state.ws_result_bytes:
        m = st.session_state.ws_metrics or {}
        banner("ok", "Traducción completada",
               body=f"Tu archivo está listo. {m.get('lines', '—')} líneas procesadas.")
        metrics([
            (m.get("cpl_rate", "—"),  "Tasa CPL",  True),
            (m.get("lines", "—"),     "Líneas",    True),
            (m.get("langs", "—"),     "Idiomas",   False),
        ])
        st.markdown('<div style="margin-top:14px;"></div>', unsafe_allow_html=True)
        st.download_button(
            "↓  Descargar SRT traducido",
            data=st.session_state.ws_result_bytes,
            file_name=st.session_state.ws_result_name,
            mime="text/plain",
            use_container_width=True,
        )

    if st.session_state.ws_state == "error" and st.session_state.ws_error:
        banner(
            "err", "Error en la traducción",
            body_html=(
                f'{escape(st.session_state.ws_error)}<br>'
                f'<span class="mono">uvicorn app.main:app --reload</span>'
            ),
        )

# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · GLOSARIO
# ═══════════════════════════════════════════════════════════════════════════
def render_glosario():
    page_header(
        "Glosario y Reglas RAG",
        "Términos y traducciones fijas que la IA aplicará como contexto vectorial.",
    )

    # ── Lectura desde la API en cada render (fuente de verdad: SQLite) ──
    glossary = api_get_glossary()

    col_form, col_table = st.columns([1, 1.5], gap="large")

    with col_form:
        with st.form("add_rule", clear_on_submit=True):
            st.markdown('<div class="fcard-title">Nueva regla</div>', unsafe_allow_html=True)
            term  = st.text_input("Término original",       placeholder="Ej: Walter White")
            trans = st.text_input("Traducción obligatoria", placeholder="Ej: Walter White")
            ctx   = st.text_area ("Contexto de uso", placeholder="Ej: No traducir, nombre propio del protagonista.", height=80)
            tag   = st.selectbox ("Etiqueta", ["nombre-propio", "marca", "acrónimo", "slang", "idiom", "término"])

            if st.form_submit_button("+ Añadir regla", use_container_width=True):
                if term.strip() and trans.strip():
                    created = api_post_glossary(
                        source=term.strip(),
                        target=trans.strip(),
                        category=tag,
                        note=ctx.strip(),
                    )
                    if created is not None:
                        api_get_glossary.clear()   # invalidar cache para ver el nuevo término
                        st.toast(f'Añadido: {created["source"]} → {created["target"]}', icon="✅")
                        st.rerun()
                else:
                    st.error("Término y Traducción son obligatorios.")

    with col_table:
        n = len(glossary)
        section_label("Reglas activas", right=f'<span class="mono" style="color:var(--text-4);font-size:12px;">{n} reglas</span>')
        if n == 0:
            empty_state("📖", "Sin reglas todavía",
                        "Añade términos desde el formulario. La IA los aplicará como contexto vectorial en cada traducción.")
        else:
            # Renombrar campos al render para mantener la UI en español.
            df = pd.DataFrame(glossary).rename(columns={
                "source":   "Término",
                "target":   "Traducción",
                "note":     "Contexto",
                "category": "Etiqueta",
            })
            df = df[["Término", "Traducción", "Contexto", "Etiqueta"]]
            st.dataframe(df, hide_index=True, use_container_width=True,
                         height=min(58 + n * 38, 460))
            st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
            if st.button("Limpiar glosario", type="secondary", use_container_width=True):
                for term in glossary:
                    api_delete_glossary(term["id"])
                api_get_glossary.clear()   # invalidar cache para refrescar a vacío
                st.toast("Glosario vaciado.", icon="🗑")
                st.rerun()

    # ── Sección Importar / Exportar CSV ───────────────────────────────────
    st.markdown('<div style="height:24px;"></div>', unsafe_allow_html=True)
    section_label("Importar / Exportar CSV")

    col_dl, col_up = st.columns(2, gap="medium")

    # Descarga ───────────────────────────────────
    with col_dl:
        st.markdown(
            '<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">'
            'Descargar glosario actual'
            '</div>',
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
        )
        st.markdown(
            '<div style="font-size:11.5px;color:var(--text-4);margin-top:6px;">'
            'Compatible con Excel. Edita las filas que quieras y vuelve a subir el archivo.'
            '</div>',
            unsafe_allow_html=True,
        )

    # Subida ─────────────────────────────────────
    with col_up:
        st.markdown(
            '<div style="font-size:12.5px;font-weight:500;color:var(--text-2);margin-bottom:6px;">'
            'Subir CSV con términos'
            '</div>',
            unsafe_allow_html=True,
        )
        csv_file = st.file_uploader(
            "csv",
            type=["csv"],
            label_visibility="collapsed",
            key="csv_glossary_up",
        )
        if csv_file is not None:
            sz = csv_file.size
            sz_str = f"{sz/1024:.1f} KB" if sz < 1_048_576 else f"{sz/1_048_576:.1f} MB"
            st.markdown(
                f'<div class="file-pill"><div class="fp-dot-gray"></div>'
                f'<div class="fp-name">{csv_file.name}</div>'
                f'<div class="fp-size">{sz_str}</div></div>',
                unsafe_allow_html=True,
            )

        if st.button(
            "+  Importar al glosario",
            disabled=(csv_file is None),
            use_container_width=True,
            key="btn_import_csv",
        ):
            result = api_import_glossary_csv(csv_file.name, csv_file.getvalue())
            api_get_glossary.clear()   # refrescar caché tras inserción
            st.session_state["glossary_import_result"] = result
            st.rerun()

    # Banner con el resultado del último import (si existe)
    res = st.session_state.get("glossary_import_result")
    if res:
        st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
        if res.get("ok"):
            n_imp = res.get("imported", 0)
            n_sk  = res.get("skipped", 0)
            errs  = res.get("errors", []) or []
            if errs:
                err_html = "<br>".join(escape(e) for e in errs[:6])
                if len(errs) > 6:
                    err_html += f"<br>… y {len(errs)-6} más"
                banner(
                    "warn",
                    f"Importados {n_imp} · omitidos {n_sk} · {len(errs)} fila(s) con error",
                    body_html=err_html,
                )
            elif n_imp > 0:
                banner(
                    "ok",
                    f"Importados {n_imp} términos nuevos",
                    body=f"Se omitieron {n_sk} duplicados ya existentes."
                         if n_sk else "Sin duplicados detectados.",
                )
            else:
                banner(
                    "warn",
                    "Sin términos nuevos",
                    body=f"Las {n_sk} filas ya estaban en el glosario.",
                )
        else:
            banner("err", "Error al importar", body=res.get("detail", "Sin detalle."))
        # Una sola visualización: limpiar al renderizar
        del st.session_state["glossary_import_result"]


# ═══════════════════════════════════════════════════════════════════════════
# PÁGINA · HISTORIAL
# ═══════════════════════════════════════════════════════════════════════════
def render_historial():
    # ── Lectura desde la API en cada render (fuente de verdad: SQLite) ──
    jobs = api_get_jobs()
    n     = len(jobs)
    n_ok  = sum(1 for j in jobs if j.get("status") == "completed")
    n_err = sum(1 for j in jobs if j.get("status") == "failed")

    page_header(
        "Historial de Proyectos",
        f"{n} proyectos · {n_ok} completados · {n_err} con error"
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

        # Estado legible con icono
        status_label = {
            "completed": "✓ Completado",
            "failed":    "✕ Error",
        }.get(j.get("status"), j.get("status", "—"))

        rows.append({
            "ID":       f"JOB-{j['id']:05d}",
            "Archivo":  j.get("filename", "—"),
            "Idiomas":  f"EN → {j.get('target_lang', 'es').upper()}",
            "CPL":      j.get("cpl", 0),
            "Líneas":   j.get("n_translations", 0),
            "% CPL":    j.get("cpl_compliance", 0.0),
            "Duración": duracion,
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
            "ID":       st.column_config.TextColumn("ID",     width="small"),
            "Archivo":  st.column_config.TextColumn("Archivo", width="large"),
            "CPL":      st.column_config.NumberColumn("CPL",   format="%d"),
            "Líneas":   st.column_config.NumberColumn("Líneas", format="%d"),
            "% CPL":    st.column_config.NumberColumn("% CPL", format="%.1f%%"),
        },
    )
    st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
    if st.button("Limpiar historial", type="secondary", use_container_width=True):
        for j in jobs:
            api_delete_job(j["id"])
        api_get_jobs.clear()   # invalidar cache para refrescar a vacío
        st.session_state.ws_state = "idle"
        st.session_state.ws_result_bytes = None
        st.toast("Historial vaciado.", icon="🗑")
        st.rerun()

# ═══════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════
if   st.session_state.page == "workspace": render_workspace()
elif st.session_state.page == "glosario":  render_glosario()
elif st.session_state.page == "historial": render_historial()
