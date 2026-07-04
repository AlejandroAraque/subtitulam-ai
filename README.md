# Subtitulam — Traducción asistida de subtítulos con GPT-4o

Herramienta para traducir archivos `.srt` del inglés al español usando
GPT-4o como motor central, con tres componentes diferenciadores:

- **RAG con Qdrant** — recupera ejemplos similares de un corpus indexado
  de traducciones previas, aportando consistencia léxica entre archivos.
- **Sliding window de 20 cues** — incluye las traducciones recientes del
  mismo archivo en cada batch, asegurando coherencia narrativa interna.
- **Glosario activo** — términos del cliente inyectados como reglas
  obligatorias en el system prompt, con CRUD por API y bulk import/export
  CSV compatible con Excel español.

Frontend en Streamlit con rediseño SaaS y módulo de evaluación con métricas
estándar (BLEU, chrF, CPL compliance, glossary compliance) sobre OPUS-100.

---

## Stack

- **Backend**: FastAPI + SQLAlchemy 2 + SQLite + Qdrant (vector store standalone).
- **Frontend**: Streamlit 1.40+ con CSS injectado.
- **LLM**: OpenAI GPT-4o (traducción) + text-embedding-3-small (RAG) +
  gpt-4o-mini (auto-context).
- **Evaluación**: sacrebleu + datasets (Hugging Face).
- **Despliegue**: Docker Compose con 3 contenedores (Qdrant + backend + frontend).
- **Gestión de dependencias**: `uv` (recomendado) o `pip`.

---

## Requisitos

- Python ≥ 3.11
- [`uv`](https://docs.astral.sh/uv/) instalado (o `pip` como fallback)
- API key de OpenAI con acceso a GPT-4o

Coste estimado: **~$1 USD por película** de 90 minutos (~$0.40 sin RAG ni
glosario, ~$1 con todas las features). Latencia: 6-15 minutos por película
según el tier de TPM de tu cuenta OpenAI.

---

## Despliegue con Docker (recomendado)

La forma más simple de tener todo el sistema corriendo en cualquier máquina
con Docker es el `docker-compose.yml` incluido. Levanta los 3 servicios
(Qdrant + backend + frontend) aislados con persistencia en volúmenes Docker.

```bash
# 1. Clonar
git clone https://github.com/AlejandroAraque/subtitulam-ai.git
cd subtitulam-ai

# 2. Configurar variables (copia la plantilla y rellena la API key)
cp .env.example .env
# edita .env y pon tu OPENAI_API_KEY=sk-...

# 3. Levantar el stack
docker compose up -d --build

# 4. Verificar (los 3 servicios deben estar UP, backend HEALTHY)
docker compose ps
```

Abre [http://localhost:8501](http://localhost:8501) para la UI.

| Servicio | URL local | Función |
|---|---|---|
| Frontend (Streamlit) | http://localhost:8501 | UI principal |
| Backend (FastAPI) | http://localhost:8000 | API + docs Swagger en `/docs` |
| Qdrant dashboard | http://localhost:6333/dashboard | Inspección visual de los vectores |

**Persistencia**: los datos sobreviven a `docker compose down`/`up` mediante
dos volúmenes Docker nombrados (`qdrant_storage` y `app_data`). Solo se
borran con `docker compose down -v` (acción consciente).

**Para Qdrant Cloud en lugar de self-hosted**: descomenta `QDRANT_URL` y
`QDRANT_API_KEY` en `.env` y comenta el servicio `qdrant` en
`docker-compose.yml`. El código del backend funciona sin cambios.

### Aceleración GPU (opcional, solo OCR)

El módulo OCR (EasyOCR) se acelera entre 5× y 10× con GPU NVIDIA. La
traducción no usa GPU local (es API). El propio `ocr_service.py` detecta
CUDA en runtime: si está, la usa; si no, cae a CPU sin romper nada.

Para **exponer la GPU al contenedor** del backend, hay un override
opcional `docker-compose.gpu.yml`:

```bash
# Sin GPU (lo normal):
docker compose up -d

# Con GPU NVIDIA disponible en el host:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

Requisitos del host para usar el override GPU:

- GPU NVIDIA con drivers recientes (los wheels de torch traen el runtime
  CUDA, no hace falta instalar el CUDA Toolkit en el host).
- `nvidia-container-toolkit` instalado para que Docker exponga la GPU al
  contenedor.

Si esos requisitos no están, el override falla al arrancar. Por eso es
un archivo separado y no parte del `docker-compose.yml` principal.

---

## Setup en local (sin Docker, para desarrollo)

Si prefieres iterar sobre el código sin rebuild de imagen cada vez:

```bash
# 1. Clonar y entrar
git clone https://github.com/AlejandroAraque/subtitulam-ai.git
cd subtitulam-ai

# 2. Instalar dependencias (uv crea el venv automáticamente)
uv sync

# 3. Configurar variables
cp .env.example .env
# edita .env y pon tu OPENAI_API_KEY=sk-...

# 4. Arrancar Qdrant en Docker (necesario — ya no usamos vector store embebido)
#    Reutiliza el servicio del compose: funciona igual en bash y PowerShell.
docker compose up -d qdrant

# 5. Arrancar el backend (puerto 8000)
uv run uvicorn app.main:app --reload

# 6. En otra terminal, arrancar el frontend (puerto 8501)
uv run streamlit run app_ui.py
```

La primera arrancada del backend crea automáticamente:

- `data/subtitulam.db` — SQLite con tablas `glossary_terms`, `jobs`, `translations`.
- La colección `translations` en Qdrant (idempotente).

Si tenías corpus indexado en ChromaDB de versiones anteriores, re-indéxalo
una sola vez en Qdrant:

```bash
uv run python scripts/backfill_qdrant.py
```

Abre [http://localhost:8501](http://localhost:8501) y sube un `.srt`.

---

## Uso

### Desde la UI (Streamlit)

1. **Workspace** — sube un `.srt`, opcionalmente añade contexto de la obra,
   activa auto-context si la obra es conocida (Breaking Bad, Friends...) y
   pulsa Traducir. Descarga el `.srt` traducido.
2. **Glosario** — gestiona términos manualmente o sube un CSV
   (`source;target;category;note`, separador `;`, UTF-8 con BOM).
3. **Historial** — lista de jobs anteriores con métricas (CPL compliance,
   tokens, latencia) y descarga del output.

### Desde la línea de comandos (showcase / batch)

```bash
# Traducción simple sin RAG ni glosario
uv run python data/showcase/translate_showcase.py \
    --version mi_run \
    --srt-path data/showcase/raw/mi_pelicula.srt \
    --no-glossary --sliding-window-size 0

# Modo completo (RAG + sliding 20 + glosario + auto-context)
uv run python data/showcase/translate_showcase.py \
    --version mi_run_full \
    --srt-path data/showcase/raw/mi_pelicula.srt \
    --rag --auto-context

# Modo "indexado" (crea Job en SQLite + indexa en Qdrant)
uv run python data/showcase/translate_showcase.py \
    --version mi_run_indexed \
    --srt-path data/showcase/raw/mi_pelicula.srt \
    --rag --index
```

Output en `data/showcase/runs/<version>_showcase_es.srt` + JSON con stats.

Flags relevantes:

| Flag | Default | Efecto |
|---|---|---|
| `--rag` | desactivado | activa retrieval desde Qdrant |
| `--sliding-window-size N` | 20 | nº de cues anteriores como contexto. 0 = off |
| `--no-glossary` | activado | desactiva inyección del glosario |
| `--auto-context` | desactivado | gpt-4o-mini deduce contexto del título |
| `--index` | desactivado | persiste el job + indexa vectores |
| `--cpl N` | 42 | límite de caracteres por línea |
| `--target-lang` | es | es / es-419 / fr / de / pt / it |

### Evaluación (BLEU / chrF / CPL / glossary)

```bash
# Evaluación sobre testset (ver flags disponibles con --help)
uv run python -m eval --help
uv run python -m eval --config baseline --rag

# A/B contra una traducción humana profesional (alineación temporal
# automática entre EN original / ES humano / ES sistema):
uv run python eval/eval_against_human.py <salida_ia.srt> \
    --en <original_en.srt> --hum <referencia_humana.srt>

# Re-traducción offline sin RAG (para A/B de prompts sin contaminación):
uv run python eval/retranslate_offline.py <original_en.srt> -o <salida.srt>
```

Resultados en `data/eval_runs/`. Para ablación entre configuraciones,
lanzar varias veces con diferentes flags y comparar.

---

## Estructura del repositorio

```
app/
├── main.py                  # Entry point FastAPI + lifespan
├── api/routes.py            # Endpoints: /translate, /glossary, /jobs, /ocr
├── core/
│   ├── config.py            # configuración central (modelo, CPL, Qdrant, versión)
│   ├── database.py          # SQLAlchemy + init_db
│   ├── job_logs.py          # buffer de logs en vivo por job (polling UI)
│   └── openai_client.py     # cliente AsyncOpenAI singleton lazy
├── models/schemas.py        # GlossaryTerm, Job, Translation (ORM)
├── services/
│   ├── translation_service.py    # build_system_prompt, translate_texts
│   ├── glossary_service.py       # CRUD + import_csv_rows
│   ├── rag_service.py            # Qdrant wrapper (query + index)
│   ├── embeddings_service.py     # OpenAI embeddings (one + batch)
│   ├── context_service.py        # auto-context con gpt-4o-mini
│   ├── history_service.py        # lifecycle de jobs
│   ├── srt_service.py            # parseo/rebuild de SRT (librería srt)
│   └── ocr_service.py            # EasyOCR + OpenCV (texto en frames)
└── utils/text_utils.py      # ajustar_cpl_optimo

app_ui.py                    # Frontend Streamlit (single file)

tests/                       # tests unitarios (pytest): métricas, parsers, CPL

docker/
├── Dockerfile.backend       # imagen del backend (FastAPI + uvicorn + libGL)
└── Dockerfile.frontend      # imagen del frontend (Streamlit)

docker-compose.yml           # stack de 3 servicios (qdrant + backend + frontend)
docker-compose.gpu.yml       # override opcional: GPU NVIDIA para el OCR
.env.example                 # plantilla de variables de entorno
LICENSE                      # MIT

data/
├── qdrant_storage/          # vector store local sin Docker (gitignored)
├── subtitulam.db            # SQLite local sin Docker (gitignored)
├── eval_runs/               # resultados de evaluación (gitignored)
├── showcase/
│   ├── selected/            # SRTs canónicos para tests
│   ├── raw/                 # SRTs de prueba personales (gitignored)
│   ├── runs/                # outputs de translate_showcase (gitignored)
│   └── translate_showcase.py    # script CLI

scripts/
└── backfill_qdrant.py       # re-indexa SQLite → Qdrant (idempotente)

eval/
├── runner.py                # evaluación BLEU/chrF sobre testset
├── cli.py                   # entry point CLI (python -m eval)
├── config.py                # configs de evaluación
├── metrics/                 # implementaciones BLEU/chrF/CPL/glossary
├── eval_against_human.py    # A/B tripartito EN / humano / IA
├── retranslate_offline.py   # re-traducción sin RAG para A/B de prompts
└── showcase_diff.py         # diff comparativo de runs del showcase

.github/workflows/ci.yml     # CI: lint + smoke imports + tests + docker build
```

---

## Configuración avanzada

### Variables de entorno

| Variable | Requerida | Default | Notas |
|---|---|---|---|
| `OPENAI_API_KEY` | sí | — | Acceso a GPT-4o + embeddings |
| `QDRANT_URL` | no | `http://localhost:6333` (local) / `http://qdrant:6333` (Docker compose) | URL del vector store |
| `QDRANT_API_KEY` | no | (vacío) | Solo si usas **Qdrant Cloud** en lugar de self-hosted |
| `BACKEND_URL` | no | `http://localhost:8000` (local) / `http://backend:8000` (Docker compose) | URL que el frontend usa para hablar con el backend |
| `DATABASE_URL` | no | `sqlite:///data/subtitulam.db` (local) / `sqlite:////app/data/subtitulam.db` (Docker compose) | Override para Postgres si migras |
| `OPENAI_MODEL` | no | `gpt-4o` | Modelo de traducción (útil para ablations) |
| `OPENAI_TEMPERATURE` | no | `0.3` | Temperatura de la traducción |
| `OPENAI_MAX_TOKENS` | no | `800` | Máx tokens de respuesta por chunk |
| `DEFAULT_CHUNK_SIZE` | no | `5` | Cues por llamada al LLM |
| `DEFAULT_CPL_LIMIT` | no | `38` | Límite de caracteres por línea (38 = UNE 153010 TV; 42 = Netflix) |
| `DEFAULT_TARGET_LANG` | no | `es` | Idioma destino por defecto |

### Actualizar una instalación existente

Con la carpeta conectada al repositorio (clonada con git), actualizar a la
última versión publicada es un solo comando:

```powershell
.\scripts\actualizar.ps1
```

El script hace `git pull` + rebuild + reinicio y comprueba el estado. Los
datos (historial, glosario, memoria de traducción) viven en volúmenes
Docker y no se tocan.

**Si la instalación se hizo desde un paquete portable** (zip del código,
sin git), conectarla al repositorio una sola vez — desde la carpeta del
proyecto, con Git instalado:

```powershell
git init
git remote add origin https://github.com/AlejandroAraque/subtitulam-ai.git
git fetch origin
git reset --hard origin/main
git branch --set-upstream-to=origin/main main
```

(La última línea vincula la rama local con la remota — sin ella,
`git pull` pide configurar el tracking.) El `.env` y los volúmenes de
datos sobreviven (git no los gestiona). A partir de ahí,
`.\scripts\actualizar.ps1` para cada actualización.

**Actualización automática (recomendado para instalaciones de usuario
final)**: una tarea programada de Windows puede ejecutar el script cada
noche, de modo que la instalación siempre esté al día sin intervención:

```powershell
# Ejecutar UNA vez como administrador, desde la carpeta del proyecto:
$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\scripts\actualizar.ps1`""
$momento = New-ScheduledTaskTrigger -Daily -At 03:00
Register-ScheduledTask -TaskName "Subtitulam-Actualizar" `
  -Action $accion -Trigger $momento -Description "Actualiza Subtitulam cada noche"
```

### Reseteo de la base de datos

**Local (sin Docker):**

```bash
rm data/subtitulam.db
rm -rf data/qdrant_storage/
```

El backend recrea SQLite y Qdrant recrea la colección al siguiente arranque.

**En Docker compose:**

```bash
docker compose down -v   # -v borra los volúmenes nombrados
docker compose up -d --build
```

### TPM (Tokens Per Minute) de OpenAI

Si tu cuenta es Tier 1 (TPM 30 000/min en gpt-4o), **no lances varios runs
en paralelo** — saturarás el límite y obtendrás errores 429 con cues
marcados como `[ERROR]` en el output. Lanza secuencial con `sleep 45`
entre runs si necesitas comparar configuraciones.

A partir de Tier 2 (TPM 450 000/min) el paralelismo es seguro.

---

## Limitaciones conocidas

- **El glosario no fuerza overrides duros**. Funciona como mecanismo de
  consistencia para terminología con varias traducciones aceptables.
  Nombres propios "obvios" que el modelo identifica solo (Walter, Sweden,
  jazz) los respeta sin necesidad del glosario; pero no admite reglas
  arbitrarias tipo `Tofu → Cubito` (el modelo prioriza coherencia narrativa).
- **gpt-4o con prompts >3 000 tokens tiene variabilidad** entre runs
  aparentemente idénticos (`temperature=0.3`). Algunos cues pueden
  traducirse de forma distinta en ejecuciones consecutivas.
- **Evaluación sobre material real con N pequeño**. Además de OPUS-100
  (corpus académico), hay BLEU/chrF contra traducción humana profesional
  vía `eval/eval_against_human.py`, pero de momento sobre un único corto;
  ampliar el dataset de referencia humana es trabajo en curso.
- **No hay multi-tenant ni auth**. Es una herramienta personal local.
  Para uso comercial habría que migrar SQLite → Postgres y añadir auth.

---

## Licencia

MIT — uso libre con atribución.

---

## Autor

Alejandro Araque · [github.com/AlejandroAraque](https://github.com/AlejandroAraque)
