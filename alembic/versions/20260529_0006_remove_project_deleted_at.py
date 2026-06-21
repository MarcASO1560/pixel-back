"""Remove project soft delete marker.

Revision ID: 20260529_0006
Revises: 20260529_0005
Create Date: 2026-05-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260529_0006"
down_revision: str | None = "20260529_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("projects", "deleted_at")


def downgrade() -> None:
    op.add_column("projects", sa.Column("deleted_at", sa.DateTime(), nullable=True))
