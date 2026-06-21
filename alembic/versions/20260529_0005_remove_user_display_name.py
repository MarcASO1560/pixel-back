"""Remove user display name.

Revision ID: 20260529_0005
Revises: 20260528_0004
Create Date: 2026-05-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260529_0005"
down_revision: str | None = "20260528_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("users", "display_name")


def downgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=255), nullable=True))
