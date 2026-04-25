# Baseline v1.5 — comparación cue por cue

**Job:** id=1 · 2026-04-25 15:07:16.833434 · 9.77s · CPL 42 · 100.0% compliance

**Modelo:** gpt-4o · context vacío · glosario no aplicado · sin RAG

**Tokens:** 1787 prompt + 305 completion = 2092 total


## Tabla cue por cue

| # | Inglés | Output v1.5 | Referencia humana | ¿Coincide? |
|---|---|---|---|---|
| 1 | I haven't seen you in ages, Marcus. | No te he visto en siglos, Marcus. | No te veía desde hace siglos, Marcus. | ✗ |
| 2 | You look like you've been through hell. | Parece que has pasado por un infierno. | Parece que has pasado por un infierno. | ✓ |
| 3 | Tell your sister to break a leg before the show to… | Dile a tu hermana que mucha mierda / antes del espectáculo de esta noc… | Dile a tu hermana 'mucha mierda' antes del espectáculo de esta noche. | ✗ |
| 4 | I'll meet you by the bank at noon. Bring the docum… | Te veré en el banco al mediodía. / Trae los documentos. | Te veo en el banco a mediodía. Trae los documentos. | ✗ |
| 5 | No, not the river bank — the one on Fifth Avenue. | No, no el banco del río, / el de la Quinta Avenida. | No, no la orilla del río — el de la Quinta Avenida. | ✗ |
| 6 | Oh, great. Just great. | Genial. Simplemente genial. | Oh, genial. Simplemente genial. | ✗ |
| 7 | Relax, this'll be a piece of cake. | Tranquilo, será pan comido. | Tranquilo, será pan comido. | ✓ |
| 8 | It's raining cats and dogs out there. Did you brin… | Está lloviendo a cántaros. / ¿Trajiste un paraguas? | Está lloviendo a cántaros. ¿Trajiste paraguas? | ✗ |
| 9 | The server is down. We need to reboot the firewall… | El servidor está caído. Necesitamos / reiniciar el firewall antes de l… | El servidor está caído. Hay que reiniciar el firewall antes de la reun… | ✗ |
| 10 | I'll grab a coffee at Starbucks and meet you at Pe… | Voy a por un café en Starbucks / y te veo en Penn Station. | Voy a por un café al Starbucks y te veo en Penn Station. | ✗ |
| 11 | We have a mole in the department. Someone's been l… | Tenemos un topo en el departamento. / Alguien ha estado filtrando info… | Tenemos un topo en el departamento. Alguien ha estado filtrando inform… | ✗ |
| 12 | The suspect said they would cooperate, but their l… | El sospechoso dijo que cooperaría, / pero su abogado no estuvo de acue… | El sospechoso dijo que cooperaría, pero su abogado no estuvo de acuerd… | ✗ |
| 13 | Take the light bag, not the heavy one. | Lleva la bolsa ligera, no la pesada. | Lleva la bolsa ligera, no la pesada. | ✓ |
| 14 | Your Honor, my client, Mr. O'Brien, has cooperated… | Señoría, mi cliente, Mr. O'Brien, / ha cooperado plenamente. | Señoría, mi cliente, el Sr. O'Brien, ha cooperado plenamente. | ✗ |
| 15 | I <i>knew</i> you'd come through, kid. | <i>Sabía</i> que no me fallarías, chico. | <i>Sabía</i> que no me fallarías, chico. | ✓ |
| 16 | Don't get cold feet now. | No te eches atrás ahora. | No te eches atrás ahora. | ✓ |
| 17 | If anyone asks, you were never here. Understood? | Si alguien pregunta, nunca estuviste aquí. / ¿Entendido? | Si alguien pregunta, no estuviste aquí. ¿Entendido? | ✗ |
| 18 | We need 10 milligrams of adrenaline, stat! | Necesitamos 10 miligramos de adrenalina, / ¡ya! | Necesitamos 10 miligramos de adrenalina, ¡ya! | ✗ |
| 19 | Damn it, Marcus! | ¡Maldita sea, Marcus! | ¡Maldita sea, Marcus! | ✓ |
| 20 | Whatever happens tonight... remember, I've always … | Pase lo que pase esta noche... / recuerda que siempre te he apoyado. | Pase lo que pase esta noche... recuerda, siempre te he apoyado. | ✗ |

## Resumen baseline

- **Coincidencias exactas con la referencia:** 6/20 (30.0%)
- **Diferencias:** 14/20 cues divergen del gold standard

Las diferencias no implican necesariamente error: pueden ser variaciones léxicas válidas.
La métrica de coincidencia exacta es **estricta**; v2.1 usará BLEU/BERTScore para
medir similitud semántica (más justa con paráfrasis).