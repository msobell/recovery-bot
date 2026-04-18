from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recovery.db.session import Base


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    out_edges: Mapped[list[KnowledgeEdge]] = relationship(
        "KnowledgeEdge", foreign_keys="KnowledgeEdge.source_id",
        back_populates="source", cascade="all, delete-orphan",
    )
    in_edges: Mapped[list[KnowledgeEdge]] = relationship(
        "KnowledgeEdge", foreign_keys="KnowledgeEdge.target_id",
        back_populates="target", cascade="all, delete-orphan",
    )


class KnowledgeEdge(Base):
    __tablename__ = "knowledge_graph"

    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("memories.id"), primary_key=True)
    target_id: Mapped[int] = mapped_column(Integer, ForeignKey("memories.id"), primary_key=True)
    relationship_type: Mapped[str] = mapped_column(String, primary_key=True)

    source: Mapped[Memory] = relationship("Memory", foreign_keys=[source_id], back_populates="out_edges")
    target: Mapped[Memory] = relationship("Memory", foreign_keys=[target_id], back_populates="in_edges")
