"""Add a personal pixel-art palette to users.

Revision ID: 20260912_0013
Revises: 20260911_0012
Create Date: 2026-09-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260912_0013"
down_revision: str | None = "20260911_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "pixel_art_palette",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("users", "pixel_art_palette", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "pixel_art_palette")
