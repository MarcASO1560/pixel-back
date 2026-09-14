"""Add private per-user resource editor state.

Revision ID: 20260914_0014
Revises: 20260912_0013
Create Date: 2026-09-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260914_0014"
down_revision: str | None = "20260912_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_editor_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "resource_id",
            name="uq_resource_editor_states_user_resource",
        ),
    )
    op.create_index(
        op.f("ix_resource_editor_states_resource_id"),
        "resource_editor_states",
        ["resource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resource_editor_states_user_id"),
        "resource_editor_states",
        ["user_id"],
        unique=False,
    )
    op.alter_column("resource_editor_states", "version", server_default=None)
    op.alter_column("resource_editor_states", "state", server_default=None)


def downgrade() -> None:
    op.drop_index(
        op.f("ix_resource_editor_states_user_id"),
        table_name="resource_editor_states",
    )
    op.drop_index(
        op.f("ix_resource_editor_states_resource_id"),
        table_name="resource_editor_states",
    )
    op.drop_table("resource_editor_states")
