"""Add project share link role.

Revision ID: 20260530_0010
Revises: 20260529_0009
Create Date: 2026-05-30 11:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260530_0010"
down_revision: str | None = "20260529_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_share_links",
        sa.Column("role", sa.String(length=20), nullable=False, server_default="editor"),
    )
    op.alter_column("project_share_links", "role", server_default=None)


def downgrade() -> None:
    op.drop_column("project_share_links", "role")
