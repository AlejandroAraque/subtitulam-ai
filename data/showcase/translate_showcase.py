"""
Traduce un .srt y guarda el resultado en data/showcase/runs/<version>_showcase_es.srt.

Llama a translation_service.translate_texts directamente, sin pasar por
el endpoint HTTP.

Uso básico (legacy, sin RAG ni indexado):
    uv run python data/showcase/translate_showcase.py --version v2.2_pre_rag

Con RAG activo (lee ChromaDB pero no indexa):
    uv run python data/showcase/translate_showcase.py --version v2.3 --rag

Indexar en ChromaDB (modo "warm-up"): crea Job en SQLite + indexa cada batch.
Equivale a una llamada real al endpoint /translate:
    uv run python data/showcase/translate_showcase.py --version v2.3_h1 \\
        --srt-path data/showcase/selected/showcase_en_h1.srt --rag --index

Override de SRT origen:
    --srt-path PATH    (default: data/showcase/selected/showcase_en.srt)
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

from app.core.database import SessionLocal           # noqa: E402
from app.services     import translation_service     # noqa: E402
from app.services     import history_service         # noqa: E402
from app.services     import context_service         # noqa: E402
from app.services     import glossary_service        # noqa: E402

HERE             = Path(__file__).parent
DEFAULT_SRC_PATH = HERE / "selected" / "showcase_en.srt"
RUNS_DIR         = HERE / "runs"

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


async def main_async(args) -> int:
    src_path = Path(args.srt_path)
    if not src_path.exists():
        print(f"ERROR: no existe {src_path}", file=sys.stderr)
        return 1

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"{args.version}_showcase_es.srt"
    if out_path.exists():
        print(f"AVISO: {out_path.name} ya existe — se sobrescribirá")

    cues = parse_srt(src_path)
    print(f"[1/4] Cargados {len(cues)} cues de {src_path.name}")

    # ── Auto-context (opt-in, solo si no se pasó --context manual) ───────
    effective_context = args.context
    if args.auto_context and not effective_context.strip():
        print(f"      Generando contexto automático desde el nombre…")
        effective_context = await context_service.generate_context_from_title(src_path.name)
        if effective_context:
            print(f"      Contexto: {effective_context[:120]}{'…' if len(effective_context) > 120 else ''}")
        else:
            print(f"      LLM no reconoció la obra — sin contexto auto")

    # ── Cargar glosario de SQLite (a menos que se haya pasado --no-glossary) ─
    glossary_terms: list[dict] = []
    if not args.no_glossary:
        _gdb = SessionLocal()
        try:
            glossary_terms = [t.to_dict() for t in glossary_service.list_terms(_gdb)]
        finally:
            _gdb.close()
        print(f"      Glosario: {len(glossary_terms)} término(s) cargados de SQLite")
    else:
        print(f"      Glosario: desactivado (--no-glossary)")

    # ── Modo --index: crea Job pendiente para que translate_texts indexe ─
    db = None
    job = None
    if args.index:
        db = SessionLocal()
        job = history_service.create_pending_job(
            db,
            filename=src_path.name,
            target_lang=args.target_lang,
            cpl=args.cpl,
            context=effective_context,
        )
        print(f"[2/4] Job {job.id} creado en SQLite (modo --index)")
    else:
        print(f"[2/4] Sin --index: no se persiste Job ni se indexa en ChromaDB")

    texts_dict = {c["idx"]: c["text"] for c in cues}
    print(f"[3/4] Traduciendo · target={args.target_lang} · cpl={args.cpl} "
          f"· rag={args.rag} · sliding=20 · glossary={len(glossary_terms)}")

    try:
        result = await translation_service.translate_texts(
            texts_dict,
            target_lang=args.target_lang,
            context=effective_context,
            cpl_limit=args.cpl,
            job_id=(job.id if job else None),
            filename=src_path.name,
            use_rag=args.rag,
            sliding_window_size=20,
            glossary=glossary_terms,
        )
    except Exception as e:
        if db is not None and job is not None:
            history_service.fail_job(db, job, str(e))
            db.close()
        print(f"ERROR durante la traducción: {e}", file=sys.stderr)
        return 2

    translations = result["translations"]

    # ── Si --index, completar el job (insertar Translations + status='completed') ─
    if args.index and db is not None and job is not None:
        # Calcular CPL compliance
        all_lines: list[str] = []
        for txt in translations.values():
            all_lines.extend([ln for ln in txt.split("\n") if ln.strip()])
        n_total = max(1, len(all_lines))
        n_under = sum(1 for ln in all_lines if len(ln) <= args.cpl)
        cpl_compliance = round(n_under / n_total * 100, 1)

        await history_service.complete_job(
            db,
            job=job,
            cues_source=texts_dict,
            cues_target=translations,
            elapsed_s=result["elapsed_s"],
            cpl_compliance=cpl_compliance,
            tokens_prompt=result["tokens_prompt"],
            tokens_completion=result["tokens_completion"],
        )
        db.close()
        print(f"      Job {job.id} completado · CPL compliance: {cpl_compliance}%")

    srt_out = rebuild_srt(cues, translations)
    out_path.write_text(srt_out, encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024

    print(f"[4/4] Escrito {out_path.relative_to(HERE.parent.parent)} ({size_kb:.1f} KB)")
    print(f"      Tokens prompt={result['tokens_prompt']}, completion={result['tokens_completion']}, "
          f"total={result['tokens_prompt']+result['tokens_completion']}")
    print(f"      Latencia: {result['elapsed_s']:.2f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="translate_showcase")
    parser.add_argument("--version", required=True,
                        help="Etiqueta de versión, p.ej. 'v2.3_h2_no_rag'.")
    parser.add_argument("--srt-path", default=str(DEFAULT_SRC_PATH),
                        help=f"Ruta al SRT a traducir. Default: {DEFAULT_SRC_PATH.name}")
    parser.add_argument("--context", default="",
                        help="Contexto global a inyectar en el prompt.")
    parser.add_argument("--target-lang", default="es")
    parser.add_argument("--cpl", type=int, default=42)
    parser.add_argument("--rag", action="store_true",
                        help="Activar RAG: queries a ChromaDB en cada batch.")
    parser.add_argument("--index", action="store_true",
                        help="Crear Job en SQLite e indexar las traducciones "
                             "en ChromaDB (mismo flujo que /translate).")
    parser.add_argument("--auto-context", action="store_true",
                        help="Generar contexto desde el nombre del archivo "
                             "(via gpt-4o-mini). Solo si --context está vacío.")
    parser.add_argument("--no-glossary", action="store_true",
                        help="Desactivar el glosario (por defecto se inyecta "
                             "lo que haya en SQLite). Útil para ablar.")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
