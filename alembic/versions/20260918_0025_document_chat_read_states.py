"""Persist document reading positions without exposing them through public roles.

Revision ID: 20260918_0025
Revises: 20260918_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260918_0025"
down_revision: str | None = "20260918_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive: existing deployments and conversations remain usable during rollout.
    # NULL means the original owner's project creation date. Transfers retain
    # the incoming owner's membership date before removing its membership row.
    op.add_column(
        "projects", sa.Column("owner_joined_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "document_chat_read_states",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_read_message_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "resource_id"),
        sa.CheckConstraint("last_read_message_id >= 0", name="ck_document_chat_read_nonnegative"),
    )
    op.create_index(
        "ix_document_chat_read_states_project_id", "document_chat_read_states", ["project_id"]
    )
    op.create_index(
        "ix_document_chat_messages_resource_created_id", "document_chat_messages",
        ["resource_id", "created_at", "id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE public.document_chat_read_states ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON TABLE public.document_chat_read_states FROM PUBLIC")
        op.execute("""
            DO $roles$
            DECLARE role_name text;
            BEGIN
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF to_regrole(role_name) IS NOT NULL THEN
                        EXECUTE format('REVOKE ALL ON TABLE '
                                       'public.document_chat_read_states FROM %I',
                                       role_name);
                    END IF;
                END LOOP;
            END $roles$;
        """)


def downgrade() -> None:
    op.drop_index("ix_document_chat_messages_resource_created_id", "document_chat_messages")
    op.drop_table("document_chat_read_states")
    op.drop_column("projects", "owner_joined_at")
