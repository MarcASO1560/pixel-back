"""Remove project resource soft delete marker.

Revision ID: 20260529_0007
Revises: 20260529_0006
Create Date: 2026-05-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260529_0007"
down_revision: str | None = "20260529_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("project_resources", "deleted_at")


def downgrade() -> None:
    op.add_column("project_resources", sa.Column("deleted_at", sa.DateTime(), nullable=True))
