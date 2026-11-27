"""users, documents, chunks

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            email text UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            doc_type text NOT NULL CONSTRAINT ck_documents_doc_type
                CHECK (doc_type IN ('resume','jd')),
            source text NOT NULL CONSTRAINT ck_documents_source
                CHECK (source IN ('upload','text','sample')),
            filename text,
            page_count integer,
            content_sha256 text NOT NULL,
            status text NOT NULL DEFAULT 'processing' CONSTRAINT ck_documents_status
                CHECK (status IN ('processing','ready','failed')),
            error_code text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_documents_ready ON documents (user_id, doc_type) "
        "WHERE status = 'ready'"
    )
    op.execute(
        """
        CREATE TABLE chunks (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            doc_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            doc_type text NOT NULL,
            section text NOT NULL,
            ordinal integer NOT NULL,
            text text NOT NULL,
            embedding vector(1024) NOT NULL,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    op.execute("CREATE INDEX ix_chunks_user_doc ON chunks (user_id, doc_id)")


def downgrade() -> None:
    op.execute("DROP TABLE chunks")
    op.execute("DROP TABLE documents")
    op.execute("DROP TABLE users")
