import streamlit as st
import requests
import pandas as pd
from datetime import datetime

# ==========================================
# 1. ESTÉTICA Y CONFIGURACIÓN (Look & Feel)
# ==========================================
st.set_page_config(
    page_title="Subtitulam | AI Subtitle System",
    page_icon="🎬",
    layout="wide"
)

# Inyectamos CSS para igualar la estética de las capturas que enviaste
st.markdown("""
    <style>
    .main { background-color: #f8f9fa; }
    .stButton>button {
        width: 100%;
        border-radius: 8px;
        height: 3em;
        background-color: #000000;
        color: white;
        border: none;
    }
    .stButton>button:hover {
        background-color: #333333;
        color: white;
    }
    /* Estilo para las zonas de carga */
    .stFileUploader {
        border: 2px dashed #ced4da;
        border-radius: 12px;
        padding: 10px;
        background-color: white;
    }
    /* Tabs personalizadas */
    .stTabs [data-baseweb="tab-list"] {
        gap: 24px;
        border-bottom: 1px solid #e9ecef;
    }
    .stTabs [data-baseweb="tab"] {
        height: 50px;
        background-color: transparent;
        border: none;
        color: #6c757d;
    }
    .stTabs [aria-selected="true"] {
        color: #000000 !important;
        border-bottom: 2px solid #000000 !important;
    }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 2. PERSISTENCIA TEMPORAL (Glosario e Historial)
# ==========================================
if 'glosario' not in st.session_state:
    st.session_state.glosario = pd.DataFrame([
        {"Término": "Corte a", "Traducción": "Cut to", "Contexto": "Transición"},
        {"Término": "Flashback", "Traducción": "Flashback", "Contexto": "Escena pasada"}
    ])

if 'historial' not in st.session_state:
    st.session_state.historial = pd.DataFrame(columns=["Proyecto", "Fecha", "SRT Original", "Estado"])

# ==========================================
# 3. HEADER (Subtitulam)
# ==========================================
header_col1, header_col2 = st.columns([1, 4])
with header_col1:
    st.markdown("<h1 style='margin-bottom:0;'>🎬 Subtitulam</h1>", unsafe_allow_html=True)
with header_col2:
    st.markdown("<p style='text-align:right; color:gray; margin-top:25px;'>Professional Subtitling AI Engine</p>", unsafe_allow_html=True)

st.divider()

# ==========================================
# 4. NAVEGACIÓN POR PESTAÑAS
# ==========================================
tab_main, tab_glosario, tab_historial = st.tabs(["🚀 Traducción", "📖 Glosario", "⏱️ Historial"])

# --- PESTAÑA TRADUCCIÓN ---
with tab_main:
    st.markdown("### Gestión de Proyecto")
    
    col_upload1, col_upload2 = st.columns(2)
    
    with col_upload1:
        st.markdown("**1. Subir Subtítulos (.srt)**")
        srt_file = st.file_uploader("Arrastra aquí tu archivo SRT", type=["srt"], label_visibility="collapsed")
        
    with col_upload2:
        st.markdown("**2. Subir Video Referencia (.mp4)**")
        mp4_file = st.file_uploader("Arrastra aquí tu video (Opcional)", type=["mp4"], label_visibility="collapsed")

    st.write("")
    
    if st.button("Iniciar Proceso"):
        if srt_file:
            with st.spinner("La IA de Subtitulam está traduciendo y ajustando CPL..."):
                try:
                    # Llamada a tu backend FastAPI
                    files = {"file": (srt_file.name, srt_file.getvalue(), "text/plain")}
                    response = requests.post("http://127.0.0.1:8000/translate", files=files)
                    
                    if response.status_code == 200:
                        st.success("¡Traducción Finalizada!")
                        
                        # Guardar en historial
                        new_row = {"Proyecto": srt_file.name.replace(".srt", ""), "Fecha": datetime.now().strftime("%d/%m/%Y"), "SRT Original": srt_file.name, "Estado": "Completado"}
                        st.session_state.historial = pd.concat([st.session_state.historial, pd.DataFrame([new_row])], ignore_index=True)
                        
                        st.download_button(
                            label="📥 Descargar SRT Traducido",
                            data=response.content,
                            file_name=f"Subtitulam_{srt_file.name}",
                            mime="text/plain"
                        )
                    else:
                        st.error("Error en el motor de traducción.")
                except Exception as e:
                    st.error(f"No se pudo conectar con el servidor: {e}")
        else:
            st.warning("Por favor, sube al menos el archivo SRT.")

# --- PESTAÑA GLOSARIO ---
with tab_glosario:
    st.markdown("### Alimentar Glosario")
    
    col_form, col_list = st.columns([1, 2])
    
    with col_form:
        with st.form("new_term"):
            t_orig = st.text_input("Término Original")
            t_dest = st.text_input("Traducción/Definición")
            t_ctx = st.text_input("Contexto")
            if st.form_submit_button("Añadir al Glosario"):
                if t_orig and t_dest:
                    new_entry = {"Término": t_orig, "Traducción": t_dest, "Contexto": t_ctx}
                    st.session_state.glosario = pd.concat([st.session_state.glosario, pd.DataFrame([new_entry])], ignore_index=True)
                    st.toast("Término guardado")
    
    with col_list:
        st.dataframe(st.session_state.glosario, use_container_width=True, hide_index=True)

# --- PESTAÑA HISTORIAL ---
with tab_historial:
    st.markdown("### Historial de Proyectos")
    if st.session_state.historial.empty:
        st.info("Aún no hay proyectos registrados.")
    else:
        st.table(st.session_state.historial)