"""documents.embed_model: which embedder produced a document's vectors

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows predate the column: 'unknown' never matches a real embedder, so they are
    # re-embedded on the next ingest and ignored by retrieval until then.
    op.execute("ALTER TABLE documents ADD COLUMN embed_model text NOT NULL DEFAULT 'unknown'")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN embed_model")
