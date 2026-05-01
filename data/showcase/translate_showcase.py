"""
Traduce data/showcase/selected/showcase_en.srt y guarda el resultado en
data/showcase/runs/<version>_showcase_es.srt.

Llama a translation_service.translate_texts directamente (igual que el
módulo eval), sin pasar por el endpoint HTTP. Más rápido y sin necesidad
de tener uvicorn corriendo.

Uso:
    uv run python data/showcase/translate_showcase.py --version v2.2_pre_rag
    uv run python data/showcase/translate_showcase.py --version v2.3 --context "Drama coreano: hermanas Yujin y Mingyeong"
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# Permitir importar app/ aunque el script esté bajo data/showcase/
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import translation_service  # noqa: E402

HERE     = Path(__file__).parent
SRC_PATH = HERE / "selected" / "showcase_en.srt"
RUNS_DIR = HERE / "runs"

TS_RE = re.compile(
    r'\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}'
)


def parse_srt(path: Path) -> list[dict]:
    """Devuelve una lista de cues: {idx, ts, text}. Preserva el formato."""
    text = path.read_text(encoding="utf-8")
    cues = []
    for blk in re.split(r'\n\s*\n', text):
        lines = blk.strip().splitlines()
        if len(lines) >= 3 and lines[0].strip().isdigit():
            ts = lines[1].strip()
            if TS_RE.match(ts):
                cues.append({
                    "idx":  int(lines[0]),
                    "ts":   ts,
                    "text": '\n'.join(lines[2:]).strip(),
                })
    return cues


def rebuild_srt(cues: list[dict], translations: dict[int, str]) -> str:
    """Reconstruye el SRT con las traducciones, preservando timestamps."""
    parts = []
    for c in cues:
        parts.append(str(c["idx"]))
        parts.append(c["ts"])
        parts.append(translations.get(c["idx"], c["text"]))
        parts.append("")
    return '\n'.join(parts).rstrip() + '\n'


async def main_async(version: str, context: str, target_lang: str, cpl_limit: int) -> int:
    if not SRC_PATH.exists():
        print(f"ERROR: no existe {SRC_PATH}", file=sys.stderr)
        return 1

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"{version}_showcase_es.srt"
    if out_path.exists():
        print(f"AVISO: {out_path.name} ya existe — se sobrescribirá")

    cues = parse_srt(SRC_PATH)
    print(f"[1/3] Cargados {len(cues)} cues de showcase_en.srt")

    texts_dict = {c["idx"]: c["text"] for c in cues}
    print(f"[2/3] Llamando a translation_service · target={target_lang} · cpl={cpl_limit} "
          f"· context={'(vacío)' if not context else context[:40]+'...'}")
    result = await translation_service.translate_texts(
        texts_dict,
        target_lang=target_lang,
        context=context,
        cpl_limit=cpl_limit,
    )
    translations = result["translations"]

    srt_out = rebuild_srt(cues, translations)
    out_path.write_text(srt_out, encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024

    print(f"[3/3] Escrito {out_path.relative_to(HERE.parent.parent)} ({size_kb:.1f} KB)")
    print(f"      Tokens prompt={result['tokens_prompt']}, completion={result['tokens_completion']}, "
          f"total={result['tokens_prompt']+result['tokens_completion']}")
    print(f"      Latencia: {result['elapsed_s']:.2f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="translate_showcase")
    parser.add_argument("--version", required=True,
                        help="Etiqueta de versión, p.ej. 'v2.2_pre_rag', 'v2.3', 'v2.4'.")
    parser.add_argument("--context", default="",
                        help="Contexto global a inyectar en el prompt.")
    parser.add_argument("--target-lang", default="es")
    parser.add_argument("--cpl", type=int, default=42)
    args = parser.parse_args()
    return asyncio.run(main_async(
        args.version, args.context, args.target_lang, args.cpl
    ))


if __name__ == "__main__":
    sys.exit(main())
