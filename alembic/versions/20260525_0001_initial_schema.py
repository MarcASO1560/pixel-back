"""Initial schema.

Revision ID: 20260525_0001
Revises:
Create Date: 2026-05-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260525_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

resource_type = postgresql.ENUM(
    "pixel_art",
    "pixel_animation",
    "tileset",
    "music_track",
    "sound_effect",
    name="resourcetype",
    create_type=False,
)

export_kind = postgresql.ENUM(
    "png",
    "gif",
    "sprite_sheet",
    "tileset_png",
    "wav",
    "ogg",
    "zip",
    "json",
    name="exportkind",
    create_type=False,
)


def upgrade() -> None:
    resource_type.create(op.get_bind(), checkfirst=True)
    export_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("email", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("display_name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "projects",
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("thumbnail_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_opened_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_projects_owner_id"), "projects", ["owner_id"], unique=False)

    op.create_table(
        "project_folders",
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("color", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["project_folders.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_project_folders_parent_id"),
        "project_folders",
        ["parent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_folders_project_id"),
        "project_folders",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "project_resources",
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("type", resource_type, nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("thumbnail_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("color", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("folder_id", sa.Uuid(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["folder_id"], ["project_folders.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_project_resources_folder_id"),
        "project_resources",
        ["folder_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_resources_project_id"),
        "project_resources",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "resource_exports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("kind", export_kind, nullable=False),
        sa.Column("file_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("mime_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_resource_exports_resource_id"),
        "resource_exports",
        ["resource_id"],
        unique=False,
    )

    op.create_table(
        "resource_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("label", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("is_autosave", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["resource_id"], ["project_resources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_resource_revisions_resource_id"),
        "resource_revisions",
        ["resource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_resource_revisions_resource_id"), table_name="resource_revisions")
    op.drop_table("resource_revisions")

    op.drop_index(op.f("ix_resource_exports_resource_id"), table_name="resource_exports")
    op.drop_table("resource_exports")

    op.drop_index(op.f("ix_project_resources_project_id"), table_name="project_resources")
    op.drop_index(op.f("ix_project_resources_folder_id"), table_name="project_resources")
    op.drop_table("project_resources")

    op.drop_index(op.f("ix_project_folders_project_id"), table_name="project_folders")
    op.drop_index(op.f("ix_project_folders_parent_id"), table_name="project_folders")
    op.drop_table("project_folders")

    op.drop_index(op.f("ix_projects_owner_id"), table_name="projects")
    op.drop_table("projects")

    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")

    export_kind.drop(op.get_bind(), checkfirst=True)
    resource_type.drop(op.get_bind(), checkfirst=True)
