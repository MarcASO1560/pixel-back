"""Add user avatar URL.

Revision ID: 20260525_0002
Revises: 20260525_0001
Create Date: 2026-05-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260525_0002"
down_revision: str | None = "20260525_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_url")
