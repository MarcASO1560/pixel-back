"""Expire share links, persist project blocks, and rotate private presence rooms.

Revision ID: 20260918_0021
Revises: 20260918_0020
"""

from collections.abc import Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260918_0021"
down_revision: str | None = "20260918_0020"
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
    SELECT EXISTS (
        SELECT 1
        FROM public.projects AS project
        WHERE channel_topic = 'project:' || project.id::text || ':presence:'
                              || project.realtime_generation::text
          AND NOT EXISTS (
              SELECT 1 FROM public.project_blocked_users AS blocked
              WHERE blocked.project_id = project.id
                AND blocked.user_id::text = coalesce(
                    nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub', ''
                )
          )
          AND (
              project.owner_id::text = coalesce(
                  nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub', ''
              )
              OR EXISTS (
                  SELECT 1 FROM public.project_members AS member
                  WHERE member.project_id = project.id
                    AND member.user_id::text = coalesce(
                        nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub', ''
                    )
              )
          )
    );
$function$;
"""


def upgrade() -> None:
    # Existing URLs retain their previous non-expiring behavior. Only newly
    # created links receive the API's seven-day default.
    op.add_column("project_share_links", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "projects",
        sa.Column(
            "realtime_generation",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.create_table(
        "project_blocked_users",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("blocked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["blocked_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_index("ix_project_blocked_users_user_id", "project_blocked_users", ["user_id"])
    op.execute("ALTER TABLE public.project_blocked_users ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON TABLE public.project_blocked_users FROM PUBLIC")
    op.execute("""
        DO $roles$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF to_regrole(role_name) IS NOT NULL THEN
                    EXECUTE format('REVOKE ALL ON TABLE public.project_blocked_users FROM %I',
                                   role_name);
                END IF;
            END LOOP;
        END $roles$;
    """)
    op.execute(PROJECT_ACCESS_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION public.can_access_realtime_project(text) FROM PUBLIC")
    op.execute("""
        DO $roles$
        BEGIN
            IF to_regrole('authenticated') IS NOT NULL THEN
                GRANT EXECUTE ON FUNCTION public.can_access_realtime_project(text) TO authenticated;
            END IF;
        END $roles$;
    """)


def downgrade() -> None:
    # Restore the preceding function before removing columns it references.
    previous_path = Path(__file__).with_name("20260914_0016_project_presence.py")
    spec = spec_from_file_location("previous_project_presence_migration", previous_path)
    assert spec is not None and spec.loader is not None
    previous = module_from_spec(spec)
    spec.loader.exec_module(previous)
    op.execute(previous.PROJECT_ACCESS_FUNCTION)
    op.drop_table("project_blocked_users")
    op.drop_column("projects", "realtime_generation")
    op.drop_column("project_share_links", "expires_at")
