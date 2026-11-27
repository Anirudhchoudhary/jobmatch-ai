"""suggestion_sets: the last bullet suggestions per analysis

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE suggestion_sets (
            analysis_id uuid PRIMARY KEY REFERENCES analyses (id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            result jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE suggestion_sets")
