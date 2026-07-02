"""
Re-indexa en Qdrant todas las translations que viven actualmente en SQLite.

Útil después de migrar de ChromaDB a Qdrant (v3.0) o tras un reset de la
colección. Idempotente: puedes correrlo varias veces sin duplicar vectores
porque rag_service.add_translations usa UUID5 determinístico de
(job_id, cue_idx) como id.

Uso:
    uv run python scripts/backfill_qdrant.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Permitir importar app/ aunque el script esté bajo scripts/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models.schemas import Job  # noqa: E402
from app.services import rag_service  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        # Solo jobs completados: los failed no tienen traducciones útiles y
        # los running están a medias.
        stmt = select(Job).where(Job.status == "completed").order_by(Job.id)
        jobs = list(db.scalars(stmt).all())
        print(f"[1/3] Jobs completados en SQLite: {len(jobs)}")

        if not jobs:
            print("      No hay nada que indexar.")
            return 0

        # Mostrar resumen previo
        n_translations_total = sum(len(j.translations) for j in jobs)
        print(f"[2/3] Translations totales a indexar: {n_translations_total}")
        for j in jobs:
            n = len(j.translations)
            if n > 0:
                print(f"      job={j.id:>3} · {j.filename:<40} · {n} cues")

        if args.dry_run:
            print("[3/3] --dry-run: no se ha indexado nada.")
            return 0

        # Indexar job a job — add_translations es idempotente.
        # Se filtran cues "[ERROR]" (chunks que fallaron, p. ej. por 429):
        # indexarlas contaminaría el corpus RAG con pares basura que luego
        # reaparecen como "EJEMPLOS PREVIOS" en jobs futuros. Es el mismo
        # filtro que aplica la indexación per-batch en translate_texts.
        # Se reconstruyen las cues vecinas (prev/next) y se adjunta el
        # contexto del job — el mismo payload enriquecido que genera la
        # indexación per-batch desde v3.5.1.
        t0 = time.time()
        n_total = 0
        n_skipped = 0
        for j in jobs:
            by_idx = {t.cue_idx: t.source_text for t in j.translations}
            payload = [
                {
                    "cue_idx":     t.cue_idx,
                    "source_text": t.source_text,
                    "target_text": t.target_text,
                    "target_lang": j.target_lang,
                    "prev_text":   by_idx.get(t.cue_idx - 1, ""),
                    "next_text":   by_idx.get(t.cue_idx + 1, ""),
                    "context":     (j.context or "").strip(),
                }
                for t in j.translations
                if t.target_text and not t.target_text.startswith("[ERROR]")
            ]
            n_skipped += len(j.translations) - len(payload)
            if not payload:
                continue
            n = await rag_service.add_translations(
                job_id=j.id,
                translations=payload,
                filename=j.filename,
            )
            n_total += n

        dt = time.time() - t0
        print(f"[3/3] Indexado completo · {n_total} translations · {dt:.1f}s")
        if n_skipped:
            print(f"      {n_skipped} cues [ERROR] o vacías excluidas del corpus.")

        # Verificación final con Qdrant
        in_qdrant = rag_service.count()
        print(f"      Qdrant reporta {in_qdrant} vectores en la colección "
              f"'{rag_service.COLLECTION}'.")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="backfill_qdrant")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="No indexa nada, solo lista lo que haría.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
