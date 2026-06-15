"""Re-traduce un SRT inglés con el prompt actual SIN RAG (A/B controlado).

Por qué sin RAG: el corpus indexado en Qdrant puede contener traducciones
de versiones anteriores del prompt; al recuperarlas como ejemplos
contaminaría la comparación. `use_rag=False` mide el efecto puro del prompt.

El glosario SÍ se aplica (se lee del backend vía HTTP, mismo estado que UI).

Uso:
  python eval/retranslate_offline.py <input_en.srt> [-o <output.srt>]

  Sin -o, escribe junto al input con prefijo `retrans_`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()  # OPENAI_API_KEY desde .env local

from app.services import (  # noqa: E402  (después de sys.path.insert)
    srt_service,
    translation_service,
)


def _parse_args() -> tuple[Path, Path]:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="SRT en inglés a re-traducir")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="ruta de salida (default: junto al input con prefijo retrans_)",
    )
    args = parser.parse_args()
    src = args.input if args.input.is_absolute() else (Path.cwd() / args.input)
    if not src.is_file():
        parser.error(f"input no encontrado: {src}")
    dst = args.output if args.output else (src.parent / f"retrans_{src.name}")
    if dst.is_dir() or not dst.suffix:
        parser.error(f"output debe ser un archivo .srt, no un directorio: {dst}")
    return src, dst


async def main() -> None:
    src, dst = _parse_args()
    print(f"[info] input : {src}")
    print(f"[info] output: {dst}")
    text = src.read_text(encoding="utf-8-sig")
    cues = srt_service.parse_srt(text)
    cues_source = {s.index: s.content for s in cues}
    print(f"[info] {len(cues_source)} cues a traducir")

    # Glosario actual del backend (a través del endpoint HTTP, así
    # usamos el mismo estado que la UI). La BBDD viva del backend está
    # en un volumen Docker, no accesible directamente con SessionLocal.
    try:
        r = requests.get("http://localhost:8000/glossary", timeout=10)
        r.raise_for_status()
        glossary_terms = r.json()
    except Exception as e:
        print(f"[warn] no se pudo leer el glosario del backend: {e}")
        glossary_terms = []
    print(f"[info] glosario del backend: {len(glossary_terms)} terminos")

    result = await translation_service.translate_texts(
        cues_source,
        target_lang="es",
        context="",
        cpl_limit=38,
        job_id=None,        # sin job_id => no indexa al RAG
        filename=src.name,
        use_rag=False,      # sin RAG => prompt puro
        sliding_window_size=20,
        glossary=glossary_terms,
        job_uuid="",
    )

    translated = result["translations"]
    final_srt = srt_service.rebuild_srt(
        cues, [translated.get(s.index, s.content) for s in cues]
    )
    dst.write_text(final_srt, encoding="utf-8")

    print(f"\n[done] {dst}")
    print(f"  elapsed: {result['elapsed_s']:.1f}s")
    print(f"  tokens prompt={result['tokens_prompt']} completion={result['tokens_completion']}")


if __name__ == "__main__":
    asyncio.run(main())
