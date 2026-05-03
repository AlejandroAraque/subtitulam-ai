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
docker run -d --name subtitulam-qdrant -p 6333:6333 \
    -v $(pwd)/data/qdrant_storage:/qdrant/storage \
    qdrant/qdrant:latest

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
uv run python -m eval.runner \
    --dataset opus100 \
    --n-samples 50 \
    --rag
```

Resultados en `logs/eval/`. Para ablación entre configuraciones, lanzar
varias veces con diferentes flags y comparar.

---

## Estructura del repositorio

```
app/
├── main.py                  # Entry point FastAPI + lifespan
├── api/routes.py            # Endpoints: /translate, /glossary, /jobs
├── core/database.py         # SQLAlchemy + init_db
├── models/schemas.py        # GlossaryTerm, Job, Translation
├── services/
│   ├── translation_service.py    # build_system_prompt, translate_texts
│   ├── glossary_service.py       # CRUD + import_csv_rows
│   ├── rag_service.py            # Qdrant wrapper (query + index)
│   ├── embedding_service.py      # OpenAI embeddings
│   ├── context_service.py        # auto-context con gpt-4o-mini
│   └── history_service.py        # lifecycle de jobs
└── utils/text_utils.py      # ajustar_cpl_optimo, srt parsing

app_ui.py                    # Frontend Streamlit (single file)

docker/
├── Dockerfile.backend       # imagen del backend (FastAPI + uvicorn)
└── Dockerfile.frontend      # imagen del frontend (Streamlit)

docker-compose.yml           # stack de 3 servicios (qdrant + backend + frontend)
.env.example                 # plantilla de variables de entorno

data/
├── qdrant_storage/          # vector store local sin Docker (gitignored)
├── subtitulam.db            # SQLite local sin Docker (gitignored)
├── showcase/
│   ├── selected/            # SRTs canónicos para tests
│   ├── raw/                 # SRTs de prueba personales (gitignored)
│   ├── runs/                # outputs de translate_showcase (gitignored)
│   └── translate_showcase.py    # script CLI

scripts/
└── backfill_qdrant.py       # re-indexa SQLite → Qdrant (idempotente)

eval/
├── runner.py                # evaluación BLEU/chrF sobre dataset
├── cli.py                   # entry point CLI
├── config.py                # configs de evaluación
├── metrics/                 # implementaciones BLEU/chrF/CPL/glossary
└── showcase_diff.py         # diff comparativo de runs del showcase
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

El resto de configuración (modelo, temperatura, batch size, etc.) vive en
código o como flags del script CLI.

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
- **Sin BLEU/chrF sobre material real**. Las métricas rigurosas se calculan
  solo sobre OPUS-100 (corpus académico). Sobre películas reales la
  evaluación es cualitativa (diff con baseline + glossary compliance + CPL).
- **No hay multi-tenant ni auth**. Es una herramienta personal local.
  Para uso comercial habría que migrar SQLite → Postgres y añadir auth.

---

## Licencia

MIT — uso libre con atribución.

---

## Autor

Alejandro Araque · [github.com/AlejandroAraque](https://github.com/AlejandroAraque)
