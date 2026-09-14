"""Authorize private Supabase Presence channels for project members.

Revision ID: 20260914_0016
Revises: 20260914_0015
Create Date: 2026-09-14 13:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260914_0016"
down_revision: str | None = "20260914_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROJECT_ACCESS_FUNCTION = """
CREATE OR REPLACE FUNCTION public.can_access_realtime_project(channel_topic text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $function$
    SELECT
        split_part(channel_topic, ':', 1) = 'project'
        AND split_part(channel_topic, ':', 3) = 'presence'
        AND EXISTS (
            SELECT 1
            FROM public.projects AS project
            WHERE project.id::text = split_part(channel_topic, ':', 2)
              AND (
                  project.owner_id::text = coalesce(
                      nullif(current_setting('request.jwt.claims', true), '')::jsonb
                          ->> 'sub',
                      ''
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM public.project_members AS member
                      WHERE member.project_id = project.id
                        AND member.user_id::text = coalesce(
                            nullif(current_setting('request.jwt.claims', true), '')::jsonb
                                ->> 'sub',
                            ''
                        )
                  )
              )
        );
$function$;
"""


PRESENCE_POLICIES = """
DO $block$
BEGIN
    IF to_regclass('realtime.messages') IS NULL
       OR to_regrole('authenticated') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'GRANT EXECUTE ON FUNCTION public.can_access_realtime_project(text) '
            'TO authenticated';
    EXECUTE 'DROP POLICY IF EXISTS "Project members read realtime presence" '
            'ON realtime.messages';
    EXECUTE 'DROP POLICY IF EXISTS "Project members write realtime presence" '
            'ON realtime.messages';
    EXECUTE $policy$
        CREATE POLICY "Project members read realtime presence"
        ON realtime.messages
        FOR SELECT
        TO authenticated
        USING (
            extension = 'presence'
            AND public.can_access_realtime_project(realtime.topic())
        )
    $policy$;
    EXECUTE $policy$
        CREATE POLICY "Project members write realtime presence"
        ON realtime.messages
        FOR INSERT
        TO authenticated
        WITH CHECK (
            extension = 'presence'
            AND public.can_access_realtime_project(realtime.topic())
        )
    $policy$;
END;
$block$;
"""


DROP_PRESENCE_POLICIES = """
DO $block$
BEGIN
    IF to_regclass('realtime.messages') IS NOT NULL THEN
        EXECUTE 'DROP POLICY IF EXISTS "Project members read realtime presence" '
                'ON realtime.messages';
        EXECUTE 'DROP POLICY IF EXISTS "Project members write realtime presence" '
                'ON realtime.messages';
    END IF;
END;
$block$;
"""


def upgrade() -> None:
    op.execute(PROJECT_ACCESS_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION public.can_access_realtime_project(text) FROM PUBLIC")
    op.execute(PRESENCE_POLICIES)


def downgrade() -> None:
    op.execute(DROP_PRESENCE_POLICIES)
    op.execute("DROP FUNCTION IF EXISTS public.can_access_realtime_project(text)")
