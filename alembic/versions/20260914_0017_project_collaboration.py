"""Authorize private Supabase Broadcast channels for project collaboration.

Revision ID: 20260914_0017
Revises: 20260914_0016
Create Date: 2026-09-14 14:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260914_0017"
down_revision: str | None = "20260914_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BROADCAST_POLICIES = """
DO $block$
BEGIN
    IF to_regclass('realtime.messages') IS NULL
       OR to_regrole('authenticated') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'DROP POLICY IF EXISTS "Project members read realtime collaboration" '
            'ON realtime.messages';
    EXECUTE 'DROP POLICY IF EXISTS "Project members write realtime collaboration" '
            'ON realtime.messages';
    EXECUTE $policy$
        CREATE POLICY "Project members read realtime collaboration"
        ON realtime.messages
        FOR SELECT
        TO authenticated
        USING (
            extension = 'broadcast'
            AND public.can_access_realtime_project(realtime.topic())
        )
    $policy$;
    EXECUTE $policy$
        CREATE POLICY "Project members write realtime collaboration"
        ON realtime.messages
        FOR INSERT
        TO authenticated
        WITH CHECK (
            extension = 'broadcast'
            AND public.can_access_realtime_project(realtime.topic())
        )
    $policy$;
END;
$block$;
"""


DROP_BROADCAST_POLICIES = """
DO $block$
BEGIN
    IF to_regclass('realtime.messages') IS NOT NULL THEN
        EXECUTE 'DROP POLICY IF EXISTS "Project members read realtime collaboration" '
                'ON realtime.messages';
        EXECUTE 'DROP POLICY IF EXISTS "Project members write realtime collaboration" '
                'ON realtime.messages';
    END IF;
END;
$block$;
"""


def upgrade() -> None:
    op.execute(BROADCAST_POLICIES)


def downgrade() -> None:
    op.execute(DROP_BROADCAST_POLICIES)
