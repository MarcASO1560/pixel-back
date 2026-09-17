"""Add shared image history and permanent canvas-coordinate lineage.

Revision ID: 20260918_0020
Revises: 20260917_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260918_0020"
down_revision: str | None = "20260917_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "image_operation_receipts", sa.Column("coordinate_width", sa.Integer(), nullable=True)
    )
    op.add_column(
        "image_operation_receipts", sa.Column("coordinate_height", sa.Integer(), nullable=True)
    )
    op.add_column(
        "image_operation_receipts", sa.Column("action_kind", sa.String(16), nullable=True)
    )
    op.create_table(
        "image_history_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_revision", sa.Integer(), nullable=False),
        sa.Column("latest_edit_revision", sa.Integer(), nullable=False),
        sa.Column("history_group_id", sa.String(80), nullable=True),
        sa.Column("action_kind", sa.String(16), nullable=False),
        sa.Column("before_document", sa.LargeBinary(), nullable=False),
        sa.Column("after_document", sa.LargeBinary(), nullable=False),
        sa.Column("transforms", postgresql.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("undone_at_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", "applied_revision", name="uq_image_history_revision"),
    )
    op.create_table(
        "image_history_states",
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_action_revision", sa.Integer(), nullable=False),
        sa.Column("last_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_entry_id"], ["image_history_entries.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("resource_id"),
    )
    op.create_table(
        "image_canvas_transforms",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("from_width", sa.Integer(), nullable=False),
        sa.Column("from_height", sa.Integer(), nullable=False),
        sa.Column("to_width", sa.Integer(), nullable=False),
        sa.Column("to_height", sa.Integer(), nullable=False),
        sa.Column("offset_x", sa.Integer(), nullable=False),
        sa.Column("offset_y", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_id", "revision", "position", name="uq_image_canvas_transform"
        ),
    )
    for table in ("image_history_entries", "image_canvas_transforms"):
        op.create_index(f"ix_{table}_resource_id", table, ["resource_id"])
    for table in ("image_history_entries", "image_history_states", "image_canvas_transforms"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            DO $image_history_roles$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                    REVOKE ALL ON TABLE public.{table} FROM anon;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                    REVOKE ALL ON TABLE public.{table} FROM authenticated;
                END IF;
            END
            $image_history_roles$;
        """)


def downgrade() -> None:
    op.drop_table("image_history_states")
    op.drop_table("image_history_entries")
    op.drop_table("image_canvas_transforms")
    op.drop_column("image_operation_receipts", "action_kind")
    op.drop_column("image_operation_receipts", "coordinate_height")
    op.drop_column("image_operation_receipts", "coordinate_width")
