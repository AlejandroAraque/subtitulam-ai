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
