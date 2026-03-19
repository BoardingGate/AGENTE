import streamlit as st
import json
import io
import os
import fitz  # PyMuPDF
import docx
import pandas as pd
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# ==========================================
# 1. CONFIGURACIÓN DE PÁGINA Y ESTILOS
# ==========================================
st.set_page_config(page_title="BoardingGate Multi Agente iA", layout="wide", page_icon="✈️")

# Inyección de CSS (Dark Mode Refinado)
st.markdown("""
    <style>
    /* Fondo oscuro y texto principal */
    .stApp { background-color: #0E1117; color: #E0E0E0; font-family: 'Inter', sans-serif; }
    
    /* Títulos con acentos en Azul Eléctrico y Dorado */
    h1, h2, h3 { color: #007BFF !important; }
    .gold-accent { color: #D4AF37; font-weight: bold; }
    
    /* Tarjetas (Cards) */
    div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlock"] {
        background-color: #1A1C23;
        border-radius: 10px;
        padding: 20px;
        border: 1px solid #2B2E36;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    
    /* Botones principales */
    .stButton>button {
        background-color: #007BFF; color: white; border-radius: 8px; border: none; transition: 0.3s;
    }
    .stButton>button:hover { background-color: #D4AF37; color: black; box-shadow: 0 0 10px rgba(212,175,55,0.5); }
    
    /* Inputs y Textareas */
    .stTextInput>div>div>input, .stTextArea>div>div>textarea {
        background-color: #0E1117 !important; color: white !important; border: 1px solid #007BFF !important;
    }
    
    /* Consola de salida */
    .output-console {
        background-color: #1E1E1E; border-left: 4px solid #D4AF37; padding: 15px; border-radius: 5px; font-family: monospace; white-space: pre-wrap; color: #00FF41;
    }
    </style>
""", unsafe_allow_html=True)

st.markdown("<h1>✈️ BoardingGate <span class='gold-accent'>Multi Agente iA</span></h1>", unsafe_allow_html=True)

# ==========================================
# 2. LÓGICA DE GOOGLE DRIVE Y ESTADO
# ==========================================
class DriveManager:
    def __init__(self, creds_dict):
        self.scopes = ['https://www.googleapis.com/auth/drive']
        self.creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=self.scopes)
        self.service = build('drive', 'v3', credentials=self.creds)

    def _get_file_id(self, folder_id, filename):
        query = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
        results = self.service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        files = results.get('files', [])
        return files[0]['id'] if files else None

    def upload_json(self, folder_id, filename, data_dict):
        file_metadata = {'name': filename, 'parents': [folder_id]}
        media = MediaIoBaseUpload(io.BytesIO(json.dumps(data_dict, indent=4).encode('utf-8')), mimetype='application/json', resumable=True)
        
        file_id = self._get_file_id(folder_id, filename)
        if file_id:
            # Update existing
            self.service.files().update(fileId=file_id, media_body=media).execute()
        else:
            # Create new
            self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()

    def download_json(self, folder_id, filename):
        file_id = self._get_file_id(folder_id, filename)
        if not file_id: return {}
        
        request = self.service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: _, done = downloader.next_chunk()
        fh.seek(0)
        return json.loads(fh.read().decode('utf-8'))

# Inicializar Variables de Sesión
if "config" not in st.session_state:
    st.session_state.config = {"gemini_api": "", "drive_folder": "", "creds_dict": None}
if "data" not in st.session_state:
    st.session_state.data = {"motores": {}, "agentes": {}, "plantillas": {}}

# Sincronización con Drive
def sync_drive(action="download"):
    try:
        if st.session_state.config["creds_dict"] and st.session_state.config["drive_folder"]:
            dm = DriveManager(st.session_state.config["creds_dict"])
            folder = st.session_state.config["drive_folder"]
            files = ["motores.json", "agentes.json", "plantillas.json"]
            
            if action == "download":
                for f in files: st.session_state.data[f.split('.')[0]] = dm.download_json(folder, f)
                st.toast("✅ Datos sincronizados desde Drive")
            elif action == "upload":
                for f in files: dm.upload_json(folder, f, st.session_state.data[f.split('.')[0]])
                st.toast("☁️ Datos guardados en Drive")
    except Exception as e:
        st.error(f"Error de sincronización: {e}")

# ==========================================
# 3. PROCESAMIENTO MULTIMODAL (EXTRACTORES)
# ==========================================
def extract_text(files):
    text_content = ""
    for file in files:
        ext = file.name.split('.')[-1].lower()
        try:
            if ext == 'pdf':
                doc = fitz.open(stream=file.read(), filetype="pdf")
                for page in doc: text_content += page.get_text() + "\n"
            elif ext in ['doc', 'docx']:
                doc = docx.Document(io.BytesIO(file.read()))
                for para in doc.paragraphs: text_content += para.text + "\n"
            elif ext in ['xls', 'xlsx']:
                df = pd.read_excel(file)
                text_content += df.to_string() + "\n"
            else:
                text_content += file.read().decode('utf-8') + "\n"
        except Exception as e:
            st.warning(f"No se pudo leer {file.name}: {e}")
    return text_content

# ==========================================
# 4. ORQUESTADOR IA (GEMINI)
# ==========================================
def orquestar_y_ejecutar(motor_nombre, user_prompt, context_text):
    genai.configure(api_key=st.session_state.config["gemini_api"])
    motor = st.session_state.data["motores"][motor_nombre]
    model = genai.GenerativeModel(motor["model_id"])
    
    agentes_str = json.dumps([{k: v["rol"]} for k,v in st.session_state.data["agentes"].items()])
    plantillas_str = json.dumps([{k: v["formato"]} for k,v in st.session_state.data["plantillas"].items()])

    # FASE 1: Orquestación (Decisión)
    prompt_orquestador = f"""
    Eres un Orquestador Inteligente. Basado en la petición del usuario, debes elegir el Agente ideal y la Plantilla ideal.
    Petición: {user_prompt}
    Agentes disponibles: {agentes_str}
    Plantillas disponibles: {plantillas_str}
    
    Responde EXCLUSIVAMENTE en formato JSON: {{"agente": "nombre_agente", "plantilla": "nombre_plantilla"}}
    Si no hay un agente/plantilla ideal, devuelve las llaves con string vacío.
    """
    
    try:
        response_json = model.generate_content(prompt_orquestador).text
        response_json = response_json.replace("```json", "").replace("```", "").strip()
        decision = json.loads(response_json)
    except:
        decision = {"agente": list(st.session_state.data["agentes"].keys())[0] if st.session_state.data["agentes"] else "",
                    "plantilla": list(st.session_state.data["plantillas"].keys())[0] if st.session_state.data["plantillas"] else ""}

    # FASE 2: Ejecución
    agente_rol = st.session_state.data["agentes"].get(decision["agente"], {}).get("rol", "Asistente genérico")
    plantilla_formato = st.session_state.data["plantillas"].get(decision["plantilla"], {}).get("formato", "Formato libre")
    
    system_prompt = f"""
    INSTRUCCIÓN BASE DEL MOTOR: {motor['instruccion_base']}
    TU ROL (AGENTE): {agente_rol}
    FORMATO DE SALIDA REQUERIDO: {plantilla_formato}
    """
    
    final_prompt = f"""
    DOCUMENTOS DE CONTEXTO:
    {context_text}
    
    PETICIÓN DEL USUARIO:
    {user_prompt}
    """
    
    model_exec = genai.GenerativeModel(motor["model_id"], system_instruction=system_prompt)
    resultado = model_exec.generate_content(final_prompt).text
    return decision, resultado

# ==========================================
# 5. TABS DE NAVEGACIÓN (INTERFAZ)
# ==========================================
tab_consola, tab_motores, tab_agentes, tab_plantillas, tab_config = st.tabs([
    "🚀 Consola de Ejecución", "⚙️ Motores", "🤖 Agentes", "📄 Plantillas", "🛠️ Configuración & Tutoriales"
])

# --- TAB CONFIGURACIÓN ---
with tab_config:
    st.header("🛠️ Configuración de APIs y Conexiones")
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("1. Configuración Gemini API")
        api_key = st.text_input("Gemini API Key", type="password", value=st.session_state.config["gemini_api"])
        if st.button("Guardar API Key"):
            st.session_state.config["gemini_api"] = api_key
            st.success("API Key guardada en sesión.")
            
        with st.expander("📖 Tutorial: Cómo obtener la API Key de Gemini"):
            st.markdown("""
            **Opción 1: Versión Gratuita (Google AI Studio)**
            1. Entra en [Google AI Studio](https://aistudio.google.com/).
            2. Inicia sesión con tu cuenta de Google.
            3. En el menú lateral, haz clic en **'Get API key'**.
            4. Crea un proyecto nuevo y copia la clave generada. *(Nota: Tiene límites de cuota por minuto).*
            
            **Opción 2: Versión de Pago / Producción (Google Cloud Console)**
            1. Entra en [Google Cloud Console](https://console.cloud.google.com/).
            2. Crea un proyecto nuevo.
            3. Ve a **'Facturación' (Billing)** y asocia una tarjeta de crédito (puedes poner límites).
            4. En la barra de búsqueda superior, busca **'Generative Language API'** y habilítala.
            5. Ve a **APIs & Services > Credentials**, crea una API Key y cópiala aquí. Esto elimina los cuellos de botella.
            """)

    with col2:
        st.subheader("2. Configuración Google Drive (JSON Persistente)")
        creds_file = st.file_uploader("Sube credentials.json (Service Account)", type=['json'])
        folder_id = st.text_input("Folder ID de Drive", value=st.session_state.config["drive_folder"])
        
        if st.button("Conectar y Sincronizar Drive"):
            if creds_file and folder_id:
                st.session_state.config["creds_dict"] = json.load(creds_file)
                st.session_state.config["drive_folder"] = folder_id
                sync_drive("download")
            else:
                st.error("Faltan credenciales o el Folder ID.")

        with st.expander("📖 Tutorial: Configurar Google Drive & Service Account"):
            st.markdown("""
            **Paso a paso para conectar Drive:**
            1. En [Google Cloud Console](https://console.cloud.google.com/), selecciona tu proyecto.
            2. Busca **'Google Drive API'** y haz clic en 'Habilitar'.
            3. Ve a **APIs & Services > Credentials** > Create Credentials > **Service Account**.
            4. Ponle un nombre y créala. Una vez creada, entra en ella, ve a la pestaña **'Keys'**, dale a 'Add Key' > 'Create New Key' (Formato JSON). Se descargará el `credentials.json`. Súbelo arriba.
            5. Abre el JSON descargado y copia el email que pone `"client_email"`.
            6. Ve a tu Google Drive normal, crea una carpeta llamada 'BoardingGate App'.
            7. **¡CRÍTICO!** Haz clic derecho en la carpeta > Compartir. Pega el `client_email` de la Service Account y dale permisos de **Editor**.
            8. Copia el **Folder ID** de la URL (lo que va después de `folders/` en la URL de tu navegador) y pégalo arriba.
            """)

# --- TAB MOTORES ---
with tab_motores:
    st.header("⚙️ Gestión de Motores (Modelos IA)")
    with st.form("form_motores"):
        nombre_m = st.text_input("Nombre amigable (ej: Analista Pro)")
        model_id = st.selectbox("Model ID", ["gemini-1.5-pro-latest", "gemini-1.5-flash-latest"])
        instruccion = st.text_area("Instrucción de sistema base (Personalidad núcleo)")
        if st.form_submit_button("Guardar Motor"):
            st.session_state.data["motores"][nombre_m] = {"model_id": model_id, "instruccion_base": instruccion}
            sync_drive("upload")
            st.success("Motor guardado.")
    
    st.write("### Motores Registrados")
    st.json(st.session_state.data["motores"])

# --- TAB AGENTES ---
with tab_agentes:
    st.header("🤖 Gestión de Agentes (Roles)")
    with st.form("form_agentes"):
        nombre_a = st.text_input("Nombre del Agente (ej: Abogado Laboralista)")
        rol = st.text_area("Instrucciones de Rol detalladas")
        if st.form_submit_button("Guardar Agente"):
            st.session_state.data["agentes"][nombre_a] = {"rol": rol}
            sync_drive("upload")
            st.success("Agente guardado.")
            
    st.write("### Agentes Registrados")
    st.json(st.session_state.data["agentes"])

# --- TAB PLANTILLAS ---
with tab_plantillas:
    st.header("📄 Gestión de Plantillas (Formatos de Salida)")
    with st.form("form_plantillas"):
        nombre_p = st.text_input("Nombre de la Plantilla (ej: Reporte Financiero Markdown)")
        desc_p = st.text_area("Descripción detallada del formato (opcional si subes archivo)")
        archivo_ejemplo = st.file_uploader("O sube un documento de ejemplo (PDF, Word, Excel)", type=['pdf', 'docx', 'xlsx'])
        
        if st.form_submit_button("Guardar Plantilla"):
            texto_ejemplo = extract_text([archivo_ejemplo]) if archivo_ejemplo else ""
            formato_final = f"{desc_p}\n\nEstructura de ejemplo a imitar:\n{texto_ejemplo}"
            st.session_state.data["plantillas"][nombre_p] = {"formato": formato_final}
            sync_drive("upload")
            st.success("Plantilla guardada.")
            
    st.write("### Plantillas Registradas")
    st.write(list(st.session_state.data["plantillas"].keys()))

# --- TAB CONSOLA DE EJECUCIÓN ---
with tab_consola:
    st.header("🚀 Orquestador Multimodal")
    
    if not st.session_state.config["gemini_api"]:
        st.warning("⚠️ Ve a la pestaña de Configuración e introduce tu API Key de Gemini para comenzar.")
    else:
        if not st.session_state.data["motores"]:
            st.info("Crea al menos un 'Motor' en su pestaña correspondiente para ejecutar tareas.")
        else:
            col_ej1, col_ej2 = st.columns([1, 2])
            
            with col_ej1:
                st.subheader("Entradas")
                motor_sel = st.selectbox("Selecciona Motor IA", list(st.session_state.data["motores"].keys()))
                archivos = st.file_uploader("Documentos de Contexto (Múltiples)", accept_multiple_files=True)
                peticion = st.text_area("Petición del Usuario (¿Qué necesitas?)", height=150)
                ejecutar = st.button("⚡ Ejecutar Orquestador", use_container_width=True)

            with col_ej2:
                st.subheader("Resultado")
                if ejecutar:
                    if not peticion:
                        st.error("Por favor, escribe una petición.")
                    else:
                        with st.spinner("🧠 1/2 Analizando archivos y orquestando IA..."):
                            contexto = extract_text(archivos) if archivos else "Sin contexto adjunto."
                        
                        with st.spinner("✍️ 2/2 Generando respuesta final con el rol seleccionado..."):
                            try:
                                decision, resultado = orquestar_y_ejecutar(motor_sel, peticion, contexto)
                                
                                st.markdown(f"**Agente seleccionado por IA:** `{decision.get('agente', 'Genérico')}` | **Plantilla:** `{decision.get('plantilla', 'Libre')}`")
                                st.markdown(f"<div class='output-console'>{resultado}</div>", unsafe_allow_html=True)
                                
                                st.download_button("Descargar Respuesta", data=resultado, file_name="resultado.txt", mime="text/plain")
                                
                            except Exception as e:
                                st.error(f"Error en la ejecución de la IA: {e}")
