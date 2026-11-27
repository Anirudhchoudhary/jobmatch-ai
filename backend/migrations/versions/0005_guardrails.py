"""guardrails: event log, chunk quarantine flags, per-user usage counters

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE guardrail_events (
            id uuid PRIMARY KEY,
            request_id text NOT NULL,
            user_id uuid NULL REFERENCES users(id) ON DELETE SET NULL,
            stage text NOT NULL,
            guardrail text NOT NULL,
            provider text NOT NULL,
            action text NOT NULL,
            mode text NOT NULL,
            reason text NOT NULL,
            latency_ms integer NOT NULL,
            item_count integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_guardrail_events_stage
                CHECK (stage IN ('upload', 'chat_input', 'output')),
            CONSTRAINT ck_guardrail_events_action
                CHECK (action IN ('allow', 'redact', 'flag', 'block', 'error')),
            CONSTRAINT ck_guardrail_events_mode CHECK (mode IN ('enforce', 'monitor'))
        )
        """
    )
    op.execute("CREATE INDEX ix_guardrail_events_created ON guardrail_events (created_at)")
    op.execute("ALTER TABLE chunks ADD COLUMN quarantined boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE chunks ADD COLUMN quarantine_reason text NULL")
    op.execute(
        """
        CREATE TABLE usage_counters (
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind text NOT NULL,
            window_start timestamptz NOT NULL,
            count integer NOT NULL,
            PRIMARY KEY (user_id, kind, window_start)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE usage_counters")
    op.execute("ALTER TABLE chunks DROP COLUMN quarantine_reason")
    op.execute("ALTER TABLE chunks DROP COLUMN quarantined")
    op.execute("DROP TABLE guardrail_events")
