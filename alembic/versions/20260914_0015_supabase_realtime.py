"""Broadcast persisted events through private Supabase Realtime channels.

Revision ID: 20260914_0015
Revises: 20260914_0014
Create Date: 2026-09-14 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260914_0015"
down_revision: str | None = "20260914_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BROADCAST_FUNCTION = """
CREATE OR REPLACE FUNCTION public.broadcast_realtime_event()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $function$
BEGIN
    IF to_regprocedure('realtime.send(jsonb,text,text,boolean)') IS NULL THEN
        RETURN NEW;
    END IF;

    EXECUTE 'SELECT realtime.send($1, $2, $3, $4)'
    USING
        coalesce(NEW.data, '{}'::jsonb) || jsonb_build_object(
            'event_id', NEW.id,
            'created_at', NEW.created_at
        ),
        NEW.event,
        'user:' || NEW.user_id::text,
        true;

    RETURN NEW;
END;
$function$;
"""


AUTHORIZATION_POLICY = """
DO $block$
BEGIN
    IF to_regclass('realtime.messages') IS NULL
       OR to_regrole('authenticated') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'DROP POLICY IF EXISTS "Users receive their own realtime broadcasts" '
            'ON realtime.messages';
    EXECUTE $policy$
        CREATE POLICY "Users receive their own realtime broadcasts"
        ON realtime.messages
        FOR SELECT
        TO authenticated
        USING (
            extension = 'broadcast'
            AND realtime.topic() = (
                'user:' || coalesce(
                    nullif(current_setting('request.jwt.claims', true), '')::jsonb
                        ->> 'sub',
                    ''
                )
            )
        )
    $policy$;
END;
$block$;
"""


DROP_AUTHORIZATION_POLICY = """
DO $block$
BEGIN
    IF to_regclass('realtime.messages') IS NOT NULL THEN
        EXECUTE 'DROP POLICY IF EXISTS "Users receive their own realtime broadcasts" '
                'ON realtime.messages';
    END IF;
END;
$block$;
"""


def upgrade() -> None:
    op.execute(BROADCAST_FUNCTION)
    op.execute(
        """
        DROP TRIGGER IF EXISTS realtime_events_broadcast_insert ON public.realtime_events;
        CREATE TRIGGER realtime_events_broadcast_insert
        AFTER INSERT ON public.realtime_events
        FOR EACH ROW
        EXECUTE FUNCTION public.broadcast_realtime_event();
        """,
    )
    op.execute(AUTHORIZATION_POLICY)


def downgrade() -> None:
    op.execute(DROP_AUTHORIZATION_POLICY)
    op.execute(
        "DROP TRIGGER IF EXISTS realtime_events_broadcast_insert "
        "ON public.realtime_events",
    )
    op.execute("DROP FUNCTION IF EXISTS public.broadcast_realtime_event()")
