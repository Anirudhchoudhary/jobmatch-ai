"""semantic_cache: per-user cache of grounded chat answers

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rows are per user, so an exact scan inside the (user_id, doc_set_hash) filter is
    # enough; no HNSW index.
    op.execute(
        """
        CREATE TABLE semantic_cache (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            doc_set_hash text NOT NULL,
            embedding vector(1024) NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_semantic_cache_user_docset ON semantic_cache (user_id, doc_set_hash)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE semantic_cache")
