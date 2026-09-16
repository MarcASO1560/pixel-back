"""Add durable, resource-scoped image operation acknowledgements.

Revision ID: 20260917_0019
Revises: 20260915_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260917_0019"
down_revision: str | None = "20260915_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_operation_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", sa.String(length=80), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("applied_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_id",
            "user_id",
            "operation_id",
            name="uq_image_operation_receipts_resource_user_operation",
        ),
    )
    op.create_index(
        "ix_image_operation_receipts_resource_id",
        "image_operation_receipts",
        ["resource_id"],
    )
    op.create_index(
        "ix_image_operation_receipts_user_id",
        "image_operation_receipts",
        ["user_id"],
    )
    # Receipts are backend-only acknowledgements, not a new public Supabase
    # API surface. Owners/service roles can access them; browser roles cannot.
    op.execute("ALTER TABLE public.image_operation_receipts ENABLE ROW LEVEL SECURITY")
    op.execute("""
        DO $receipt_roles$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE public.image_operation_receipts FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE public.image_operation_receipts FROM authenticated;
            END IF;
        END
        $receipt_roles$;
    """)


def downgrade() -> None:
    op.drop_table("image_operation_receipts")
