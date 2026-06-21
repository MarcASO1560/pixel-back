"""create realtime events

Revision ID: 20260530_0011
Revises: 20260530_0010
Create Date: 2026-05-30 13:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260530_0011"
down_revision: str | None = "20260530_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "realtime_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column(
            "data",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_realtime_events_created_at",
        "realtime_events",
        ["created_at"],
        unique=False,
    )
    op.create_index("ix_realtime_events_event", "realtime_events", ["event"], unique=False)
    op.create_index(
        "ix_realtime_events_user_id",
        "realtime_events",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_realtime_events_user_id_id",
        "realtime_events",
        ["user_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_realtime_events_user_id_id", table_name="realtime_events")
    op.drop_index("ix_realtime_events_user_id", table_name="realtime_events")
    op.drop_index("ix_realtime_events_event", table_name="realtime_events")
    op.drop_index("ix_realtime_events_created_at", table_name="realtime_events")
    op.drop_table("realtime_events")
