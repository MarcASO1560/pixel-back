"""Add bundled sticker identifiers to durable document conversations.

Revision ID: 20260918_0023
Revises: 20260918_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260918_0023"
down_revision: str | None = "20260918_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable and no backfill: existing text messages and the previous backend
    # remain compatible while the API and its clients are deployed in order.
    op.add_column("document_chat_messages", sa.Column("sticker_id", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("document_chat_messages", "sticker_id")
