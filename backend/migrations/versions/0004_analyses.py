"""analyses: cached match results per (resume, JD, analyst), cascading with the documents

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analyses (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            resume_doc_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            jd_doc_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            analyst text NOT NULL,
            result jsonb NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_analyses_docs UNIQUE (user_id, resume_doc_id, jd_doc_id, analyst)
        )
        """
    )
    op.execute("CREATE INDEX ix_analyses_user ON analyses (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE analyses")
