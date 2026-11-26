"""ORM models: users, documents, chunks (pgvector + full-text)."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("doc_type IN ('resume','jd')", name="ck_documents_doc_type"),
        CheckConstraint("source IN ('upload','text','sample')", name="ck_documents_source"),
        CheckConstraint("status IN ('processing','ready','failed')", name="ck_documents_status"),
        Index(
            "uq_documents_ready",
            "user_id",
            "doc_type",
            unique=True,
            postgresql_where=text("status = 'ready'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column()
    content_sha256: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="processing")
    error_code: Mapped[str | None] = mapped_column(Text)
    # provider:model:dims of the embedder that produced this document's chunk vectors.
    embed_model: Mapped[str] = mapped_column(Text, server_default="unknown")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_user_doc", "user_id", "doc_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Analysis(Base):
    """A match analysis per (resume doc, JD doc, analyst); cascades away with either document."""

    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "resume_doc_id", "jd_doc_id", "analyst", name="uq_analyses_docs"
        ),
        Index("ix_analyses_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    resume_doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    jd_doc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    analyst: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
