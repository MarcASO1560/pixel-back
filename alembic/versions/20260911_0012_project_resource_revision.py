"""Add optimistic revision to project resources.

Revision ID: 20260911_0012
Revises: 20260530_0011
Create Date: 2026-09-11 20:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260911_0012"
down_revision: str | None = "20260530_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_resources",
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("project_resources", "revision")
