"""
Capa de servicio del glosario — toda la lógica de BBDD para términos.
Los endpoints (routes.py) llaman aquí; nunca tocan SQLAlchemy directamente.
"""
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.schemas import GlossaryTerm


def list_terms(db: Session) -> List[GlossaryTerm]:
    """Devuelve todos los términos, los más recientes primero."""
    stmt = select(GlossaryTerm).order_by(GlossaryTerm.created_at.desc())
    return list(db.scalars(stmt).all())


def get_term(db: Session, term_id: int) -> Optional[GlossaryTerm]:
    """Devuelve un término por id, o None si no existe."""
    return db.get(GlossaryTerm, term_id)


def add_term(
    db: Session,
    source: str,
    target: str,
    category: str = "término",
    note: str = "",
) -> GlossaryTerm:
    """
    Crea un término nuevo y lo persiste.
    Lanza ValueError si source o target están vacíos.
    """
    source = source.strip()
    target = target.strip()
    if not source or not target:
        raise ValueError("Source y target son obligatorios.")

    term = GlossaryTerm(
        source=source,
        target=target,
        category=category.strip() or "término",
        note=note.strip(),
    )
    db.add(term)
    db.commit()
    db.refresh(term)   # rellena el id autoincrementado
    return term


def delete_term(db: Session, term_id: int) -> bool:
    """Borra un término por id. Devuelve True si existía, False si no."""
    term = db.get(GlossaryTerm, term_id)
    if term is None:
        return False
    db.delete(term)
    db.commit()
    return True


def import_csv_rows(db: Session, rows: list[dict]) -> dict:
    """Importa filas de un CSV parseado al glosario.

    Política:
      - source y target son obligatorios; rows sin ambos se cuentan en errors.
      - category default 'término', note default ''.
      - Deduplicación case-insensitive por (source, target): si ya existe
        el par, se omite (skipped). Esto hace el import idempotente.

    Returns:
        {
          "imported": int,    # filas insertadas
          "skipped":  int,    # filas omitidas por duplicado
          "errors":   list[str],  # filas con problema y por qué
        }
    """
    imported = 0
    skipped  = 0
    errors:  list[str] = []

    # Pre-cargar el glosario actual a memoria (lower-case) para dedup eficiente
    existing_pairs = {
        (t.source.strip().lower(), t.target.strip().lower())
        for t in list_terms(db)
    }

    for i, row in enumerate(rows, start=1):
        source = (row.get("source") or "").strip()
        target = (row.get("target") or "").strip()
        if not source or not target:
            errors.append(f"Fila {i}: 'source' y 'target' son obligatorios")
            continue

        key = (source.lower(), target.lower())
        if key in existing_pairs:
            skipped += 1
            continue

        category = (row.get("category") or "término").strip() or "término"
        note     = (row.get("note") or "").strip()

        try:
            term = GlossaryTerm(
                source=source,
                target=target,
                category=category,
                note=note,
            )
            db.add(term)
            existing_pairs.add(key)   # evitar duplicados intra-CSV
            imported += 1
        except Exception as e:
            errors.append(f"Fila {i}: {e}")

    db.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}
