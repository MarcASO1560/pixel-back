"""Add append-only document conversations with idempotent client identifiers.

Revision ID: 20260918_0022
Revises: 20260918_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260918_0022"
down_revision: str | None = "20260918_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_chat_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("client_message_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_id", "author_id", "client_message_id", name="uq_document_chat_client_message"
        ),
    )
    for column in ("project_id", "resource_id", "author_id"):
        op.create_index(f"ix_document_chat_messages_{column}", "document_chat_messages", [column])
    # Composite range index makes latest/history/catch-up independent of total
    # conversation size and avoids scanning messages from other documents.
    op.create_index(
        "ix_document_chat_messages_resource_id_id", "document_chat_messages", ["resource_id", "id"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE public.document_chat_messages ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON TABLE public.document_chat_messages FROM PUBLIC")
        op.execute("""
            DO $roles$
            DECLARE role_name text;
            BEGIN
                FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF to_regrole(role_name) IS NOT NULL THEN
                        EXECUTE format('REVOKE ALL ON TABLE public.document_chat_messages FROM %I',
                                       role_name);
                        EXECUTE format('REVOKE ALL ON SEQUENCE '
                                       'public.document_chat_messages_id_seq FROM %I', role_name);
                    END IF;
                END LOOP;
            END $roles$;
        """)


def downgrade() -> None:
    op.drop_table("document_chat_messages")
