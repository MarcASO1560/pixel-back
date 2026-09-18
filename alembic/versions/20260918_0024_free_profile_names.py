"""Allow longer profile names while retaining case-insensitive uniqueness.

Revision ID: 20260918_0024
Revises: 20260918_0023
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260918_0024"
down_revision: str | None = "20260918_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "username", existing_type=sa.String(40), type_=sa.String(255),
            existing_nullable=True,
        )
    # Keep the legacy unique index so the previous application remains compatible.
    op.create_index(
        "ix_users_username_case_insensitive", "users", [sa.text("lower(username)")],
        unique=True,
    )


def downgrade() -> None:
    # PostgreSQL rejects names longer than 40 rather than silently truncating them.
    op.drop_index("ix_users_username_case_insensitive", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "username", existing_type=sa.String(255), type_=sa.String(40),
            existing_nullable=True,
        )
