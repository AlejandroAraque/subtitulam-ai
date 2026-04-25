# Test-set de evaluación EN → ES

Dataset de pares (inglés_original, español_referencia_humana) usado para
medir la calidad del sistema Subtitulam.

## Formato

JSON Lines (`.jsonl`) — una línea por par, formato:

    {"id": 1, "source": "...", "target": "...", "notes": "..."}

| Campo    | Tipo | Descripción                                                  |
|----------|------|--------------------------------------------------------------|
| `id`     | int  | índice secuencial (corresponde al cue del SRT origen)        |
| `source` | str  | texto en inglés del cue original                             |
| `target` | str  | traducción humana de referencia (gold standard)              |
| `notes`  | str  | metadata: tipo de trampa lingüística, dificultad, etc.       |

## Versión actual: v1.0 (baseline)

20 pares derivados de `data/test_sample_en.srt`. Las traducciones de
referencia provienen del análisis manual realizado sobre la salida de v1.1
(commit 8cc755c): se conservan las traducciones que se validaron como
correctas y se corrigen las que mostraron problemas conocidos:

- **Cue 3** — sintaxis del idiom "break a leg" → "mucha mierda"
- **Cue 5** — pérdida del matiz "river bank" → "orilla del río"
- **Cue 15** — preservación de etiquetas HTML inline `<i>...</i>`
- **Cue 17** — corte de línea CPL respetando límites sintácticos

Cobertura del test-set inicial (16 categorías de dificultad):

| Categoría                       | Cues   |
|---------------------------------|--------|
| Idioms genéricos                | 1, 7, 8, 16, 20 |
| Idioms específicos (teatro/jurídico/médico) | 3, 14, 18 |
| Polisemia con contexto          | 4, 5, 13 |
| Sarcasmo / tono                 | 6 |
| Doble sentido                   | 11 |
| Ambigüedad de género            | 12 |
| Jerga técnica                   | 9 |
| Nombres propios y marcas        | 10, 14 |
| Formato HTML inline             | 15 |
| Interjecciones coloquiales      | 2, 19 |
| Multi-línea con corte CPL       | 17 |

## Uso (futuro v2.1 — módulo de evaluación)

Un script CLI cargará este JSONL y para cada `(source, prediction, target)`
calculará:

- **BLEU** (corpus-level)
- **BERTScore** (semántica)
- **chrF** (sensible a caracteres, bueno para SRT)
- **Adherencia al glosario** (% de términos aplicados cuando debían)
- **Cumplimiento de CPL** (% de líneas bajo el límite)

La comparación entre configuraciones (baseline / +contexto / +glosario /
+RAG) producirá la tabla de ablación de la memoria del TFM.

## Roadmap de ampliación

| Fuente                                         | Tamaño    | Calidad             | Prioridad |
|------------------------------------------------|-----------|---------------------|-----------|
| OpenSubtitles parallel corpus (opus.nlpl.eu)   | millones  | media-baja, filtrar | alta      |
| TED2020 (corpus alineado de TED)               | ~200K     | alta                | media     |
| Subtítulos profesionales propios               | pocos     | alta                | baja      |

**Objetivo memoria TFM:** 200-500 pares para resultados estadísticamente
sólidos (test estadístico bilateral con Δ-BLEU > 1.0 detectable).

## Uso programático

```python
import json

def load_testset(path="data/testset/reference_en_es.jsonl"):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

pairs = load_testset()
print(f"{len(pairs)} pares cargados")
print(pairs[0])
# → {'id': 1, 'source': 'I haven\'t seen you in ages, Marcus.', ...}
```
