"""
Modelos SQLAlchemy: las 3 tablas que usa la app.

GlossaryTerm  → glosario (lo que el usuario añade en la página "Glosario")
Job           → un trabajo de traducción (un .srt subido)
Translation   → una cue individual traducida (FK a Job) — materia prima del RAG en v2
"""
from datetime import datetime
from typing import Optional, List

from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ── 1. GlossaryTerm ───────────────────────────────────────────────────────
class GlossaryTerm(Base):
    __tablename__ = "glossary_terms"

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    source:     Mapped[str]      = mapped_column(String(256), nullable=False, index=True)
    target:     Mapped[str]      = mapped_column(String(256), nullable=False)
    category:   Mapped[str]      = mapped_column(String(64),  nullable=False, default="término")
    note:       Mapped[str]      = mapped_column(Text,        nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime,    nullable=False, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "source":     self.source,
            "target":     self.target,
            "category":   self.category,
            "note":       self.note,
            "created_at": self.created_at.isoformat(),
        }


# ── 2. Job ────────────────────────────────────────────────────────────────
class Job(Base):
    __tablename__ = "jobs"

    id:                Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename:          Mapped[str]      = mapped_column(String(512), nullable=False)
    target_lang:       Mapped[str]      = mapped_column(String(16),  nullable=False, default="es")
    cpl:               Mapped[int]      = mapped_column(Integer,     nullable=False, default=38)
    context:           Mapped[str]      = mapped_column(Text,        nullable=False, default="")

    started_at:        Mapped[datetime] = mapped_column(DateTime,    nullable=False, default=datetime.utcnow)
    elapsed_s:         Mapped[float]    = mapped_column(Float,       nullable=False, default=0.0)

    status:            Mapped[str]      = mapped_column(String(16),  nullable=False, default="completed")  # completed | failed
    cpl_compliance:    Mapped[float]    = mapped_column(Float,       nullable=False, default=0.0)          # 0..100

    tokens_prompt:     Mapped[int]      = mapped_column(Integer,     nullable=False, default=0)
    tokens_completion: Mapped[int]      = mapped_column(Integer,     nullable=False, default=0)

    error:             Mapped[str]      = mapped_column(Text,        nullable=False, default="")

    # Relación 1-N con Translation. cascade="all, delete-orphan" → si borras
    # el Job, sus translations desaparecen automáticamente.
    translations: Mapped[List["Translation"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "filename":          self.filename,
            "target_lang":       self.target_lang,
            "cpl":               self.cpl,
            "context":           self.context,
            "started_at":        self.started_at.isoformat(),
            "elapsed_s":         self.elapsed_s,
            "status":            self.status,
            "cpl_compliance":    self.cpl_compliance,
            "tokens_prompt":     self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "error":             self.error,
            "n_translations":    len(self.translations),
        }


# ── 3. Translation ────────────────────────────────────────────────────────
class Translation(Base):
    __tablename__ = "translations"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id:      Mapped[int]      = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    cue_idx:     Mapped[int]      = mapped_column(Integer, nullable=False)
    source_text: Mapped[str]      = mapped_column(Text,    nullable=False)
    target_text: Mapped[str]      = mapped_column(Text,    nullable=False)
    created_at:  Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    job: Mapped["Job"] = relationship(back_populates="translations")

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "job_id":      self.job_id,
            "cue_idx":     self.cue_idx,
            "source_text": self.source_text,
            "target_text": self.target_text,
            "created_at":  self.created_at.isoformat(),
        }
