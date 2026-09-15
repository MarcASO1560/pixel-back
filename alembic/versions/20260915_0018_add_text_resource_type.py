"""Add text project resources.

Revision ID: 20260915_0018
Revises: 20260914_0017
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260915_0018"
down_revision: str | None = "20260914_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL requires enum additions to be committed before the new value is used.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE public.resourcetype ADD VALUE IF NOT EXISTS 'text'")


def downgrade() -> None:
    # PostgreSQL cannot safely remove one enum value in place. Keeping the label is
    # backwards-compatible and avoids destructive handling of existing text resources.
    pass
