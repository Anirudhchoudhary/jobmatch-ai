"""request_log: one row per /api request (usage, cost, latency)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE request_log (
            id uuid PRIMARY KEY,
            request_id text NOT NULL,
            user_id uuid NULL REFERENCES users(id) ON DELETE SET NULL,
            route text NOT NULL,
            method text NOT NULL,
            status integer NOT NULL,
            models text[] NOT NULL DEFAULT '{}',
            input_tokens integer NOT NULL DEFAULT 0,
            output_tokens integer NOT NULL DEFAULT 0,
            cache_read_tokens integer NOT NULL DEFAULT 0,
            cache_write_tokens integer NOT NULL DEFAULT 0,
            cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
            cache_hit boolean NOT NULL DEFAULT false,
            latency_ms integer NOT NULL,
            guardrails text[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_request_log_created ON request_log (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE request_log")
