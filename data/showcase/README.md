# Showcase — SRTs reales para evaluación cualitativa

Esta carpeta contiene un **caso de prueba canónico** sobre el cual se ejecuta
cada versión del sistema. Sirve para mostrar **evolución cualitativa** de
las traducciones a lo largo del proyecto, complementando las métricas
cuantitativas del módulo `eval/`.

## Estructura

```
data/showcase/
├── README.md           ← este archivo (en git)
├── raw/                ← SRTs originales aportados por el autor
│                         (NO en git por copyright)
├── selected/           ← el SRT canónico recortado (~150 cues)
│                         (NO en git por derivar de material copyrighted)
└── runs/               ← traducciones generadas por cada versión
                          (NO en git por ser obra derivada)
```

Los archivos `.srt` están bloqueados por `.gitignore` global. Solo este
README llega al repositorio.

## Política de uso

- Los SRTs originales son material **copyrighted** y permanecen en local.
- El SRT recortado (`selected/`) es un excerpt académico (uso justo) usado
  internamente para validación; no se redistribuye.
- Los outputs por versión (`runs/`) se conservan en local para análisis
  comparativo. Las citas en la memoria del TFM aparecen entrecomilladas
  con cita de fuente y limitan la extensión.

## Flujo de trabajo

1. **Aportar materia prima**: el autor coloca SRTs reales en `raw/`.
2. **Selección**: se elige UN SRT (criterio: variedad de diálogo, riqueza
   léxica, claridad narrativa) y se recortan ~150 cues coherentes →
   `selected/showcase_en.srt`.
3. **Ejecución por versión**: cada hito mayor (v1.5, v2.1.6, v2.2, v2.3...)
   ejecuta `/translate` sobre el showcase y guarda el resultado en
   `runs/<version>_showcase_es.srt`.
4. **Análisis cualitativo**: comparación cue-a-cue entre versiones para
   identificar mejoras concretas (idiomáticas, de coherencia narrativa,
   adherencia al glosario).

## Por qué este artefacto existe

Las métricas BLEU/chrF/CPL del módulo `eval/` son números agregados que
demuestran progreso medible. Pero el tribunal del TFM querrá ver
**ejemplos concretos** de cómo el sistema mejora. El showcase proporciona
ese material narrativo: *"observe cómo en v1.5 el sistema traducía X, en
v2.3 con RAG produce Y, y la diferencia recoge la coherencia introducida
por la memoria de traducciones previas."*
