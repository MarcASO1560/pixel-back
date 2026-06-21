"""add user pixel avatar json

Revision ID: 20260528_0004
Revises: 20260527_0003
Create Date: 2026-05-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260528_0004"
down_revision: str | None = "20260527_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_pixel_art", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_pixel_art")
