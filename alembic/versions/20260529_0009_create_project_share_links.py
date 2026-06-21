"""Create project share links.

Revision ID: 20260529_0009
Revises: 20260529_0008
Create Date: 2026-05-29 22:38:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260529_0009"
down_revision: str | None = "20260529_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_share_links",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("project_id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(op.f("ix_project_share_links_token"), "project_share_links", ["token"])


def downgrade() -> None:
    op.drop_index(op.f("ix_project_share_links_token"), table_name="project_share_links")
    op.drop_table("project_share_links")
