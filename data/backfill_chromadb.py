"""
Backfill: indexa en ChromaDB todas las Translations históricas que tienes
en SQLite pero que no se indexaron en su momento (porque se crearon antes
del hook de v2.2 Fase 4).

Idempotente: ChromaDB hace upsert por id, así que ejecutar el script
varias veces no duplica entradas — solo refresca.

Uso:
    uv run python data/backfill_chromadb.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Forzar UTF-8 en stdout/stderr para que los caracteres Unicode (→ Δ)
# no exploten en consola PowerShell con cp1252. Inocuo en Linux/Mac.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

# Permitir importar app/ aunque el script esté bajo data/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models.schemas  import Job  # noqa: E402
from app.services        import rag_service  # noqa: E402


async def main_async() -> int:
    db = SessionLocal()
    try:
        # ── Estado inicial ─────────────────────────────────────────────────
        try:
            n_before = rag_service.count()
        except Exception as e:
            print(f"ERROR accediendo a ChromaDB: {e}", file=sys.stderr)
            return 1

        jobs = list(db.scalars(
            select(Job).where(Job.status == "completed").order_by(Job.id)
        ))
        print(f"[1/3] {len(jobs)} jobs 'completed' en SQLite")
        print(f"      ChromaDB tiene {n_before} vectores antes del backfill")

        # ── Indexar cada job ───────────────────────────────────────────────
        total_indexed = 0
        total_skipped = 0
        print(f"[2/3] Indexando…")
        for job in jobs:
            # Filtrar translations con source_text válido
            valid = [
                t for t in job.translations
                if t.source_text and t.source_text.strip()
            ]
            n_skipped = len(job.translations) - len(valid)
            if n_skipped:
                total_skipped += n_skipped

            if not valid:
                print(f"  job {job.id:>3} ({job.filename:<40}): sin translations válidas, skip")
                continue

            try:
                trans_data = [
                    {
                        "id":          t.id,
                        "cue_idx":     t.cue_idx,
                        "source_text": t.source_text,
                        "target_text": t.target_text,
                        "target_lang": job.target_lang,
                    }
                    for t in valid
                ]
                n = await rag_service.add_translations(job.id, trans_data)
                total_indexed += n
                print(f"  job {job.id:>3} ({job.filename[:38]:<40}): +{n} translations")
            except Exception as e:
                print(f"  job {job.id:>3} ({job.filename:<40}): FALLO — {e}", file=sys.stderr)

        # ── Resumen ────────────────────────────────────────────────────────
        n_after = rag_service.count()
        print(f"[3/3] Backfill completo")
        print(f"      Translations indexadas (este run):    {total_indexed}")
        if total_skipped:
            print(f"      Translations descartadas (vacías):    {total_skipped}")
        print(f"      ChromaDB:  {n_before} → {n_after} vectores  (Δ {n_after - n_before})")
        return 0

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
