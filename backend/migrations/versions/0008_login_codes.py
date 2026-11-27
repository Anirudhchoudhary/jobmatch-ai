"""login_codes: hashed email OTPs for sign-in

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE login_codes (
            id uuid PRIMARY KEY,
            email_hash text NOT NULL,
            ip_hash text NOT NULL,
            code_hash text NOT NULL,
            expires_at timestamptz NOT NULL,
            attempts integer NOT NULL DEFAULT 0,
            consumed_at timestamptz,
            invalidated_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_login_codes_email_created ON login_codes (email_hash, created_at)")
    op.execute("CREATE INDEX ix_login_codes_ip_created ON login_codes (ip_hash, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE login_codes")
