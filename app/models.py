from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import EmailStr, field_validator
from sqlalchemy import JSON, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.time import utc_now


def jsonb_column(name: str | None = None) -> Column:
    column_type = JSON().with_variant(JSONB, "postgresql")
    return Column(name, column_type) if name else Column(column_type)


class ResourceType(StrEnum):
    pixel_art = "pixel_art"
    pixel_animation = "pixel_animation"
    tileset = "tileset"
    music_track = "music_track"
    sound_effect = "sound_effect"


class ExportKind(StrEnum):
    png = "png"
    gif = "gif"
    sprite_sheet = "sprite_sheet"
    tileset_png = "tileset_png"
    wav = "wav"
    ogg = "ogg"
    zip = "zip"
    json = "json"


class ProjectAccessRole(StrEnum):
    viewer = "viewer"
    editor = "editor"
    owner = "owner"


class UserBase(SQLModel):
    username: str | None = Field(default=None, index=True, max_length=40)
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=2048)
    avatar_pixel_art: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class User(UserBase, table=True):
    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UserCreate(UserBase):
    pass


class UserUpdate(SQLModel):
    username: str | None = Field(default=None, min_length=3, max_length=40)
    avatar_pixel_art: dict[str, Any] | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, username: str | None) -> str | None:
        if username is None:
            return username
        if not username.replace("_", "").isalnum():
            raise ValueError("Username can only contain letters, numbers, and underscores")
        return username


class GoogleAuthSessionCreate(SQLModel):
    credential: str | None = Field(default=None, min_length=1)
    access_token: str | None = Field(default=None, min_length=1)


class EmailPasswordSessionCreate(SQLModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserRegistrationCreate(SQLModel):
    username: str = Field(min_length=3, max_length=40)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    password_confirmation: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, username: str) -> str:
        if not username.replace("_", "").isalnum():
            raise ValueError("Username can only contain letters, numbers, and underscores")
        return username


class PasswordResetRequestCreate(SQLModel):
    email: EmailStr


class PasswordResetConfirmCreate(SQLModel):
    token: str = Field(min_length=24, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    password_confirmation: str = Field(min_length=8, max_length=128)


class PasswordResetRequestPublic(SQLModel):
    status: str


class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(SQLModel):
    sub: UUID
    exp: int | None = None


class UserPublic(UserBase):
    id: UUID
    is_admin: bool = False
    created_at: datetime
    updated_at: datetime


class PasswordCredential(SQLModel, table=True):
    __tablename__ = "password_credentials"

    user_id: UUID = Field(foreign_key="users.id", primary_key=True)
    password_hash: str = Field(max_length=255)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PasswordResetToken(SQLModel, table=True):
    __tablename__ = "password_reset_tokens"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(unique=True, index=True, max_length=128)
    expires_at: datetime
    used_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ProjectBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict, sa_column=jsonb_column())
    thumbnail_url: str | None = None


class Project(ProjectBase, table=True):
    __tablename__ = "projects"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    owner_id: UUID = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_opened_at: datetime | None = None
    archived_at: datetime | None = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    settings: dict[str, Any] | None = None
    thumbnail_url: str | None = None


class ProjectPublic(ProjectBase):
    id: UUID
    owner_id: UUID
    access_role: str = ProjectAccessRole.owner
    access_count: int = 1
    created_at: datetime
    updated_at: datetime
    last_opened_at: datetime | None


class ProjectMember(SQLModel, table=True):
    __tablename__ = "project_members"

    project_id: UUID = Field(foreign_key="projects.id", primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", primary_key=True, index=True)
    role: str = Field(default="editor", max_length=20)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectShareLink(SQLModel, table=True):
    __tablename__ = "project_share_links"

    project_id: UUID = Field(foreign_key="projects.id", primary_key=True)
    token: str = Field(unique=True, index=True, max_length=128)
    role: str = Field(default=ProjectAccessRole.editor, max_length=20)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectShareLinkCreate(SQLModel):
    role: str = Field(default=ProjectAccessRole.editor, max_length=20)


class ProjectShareLinkPublic(SQLModel):
    project_id: UUID
    token: str
    url: str
    role: str
    created_at: datetime
    updated_at: datetime


class ProjectMemberUpdate(SQLModel):
    role: str = Field(min_length=1, max_length=20)


class ProjectAccessUserPublic(SQLModel):
    id: UUID
    username: str | None = None
    email: EmailStr
    avatar_url: str | None = None
    avatar_pixel_art: dict[str, Any] | None = None
    role: str
    is_owner: bool = False
    joined_at: datetime | None = None


class RealtimeEventLog(SQLModel, table=True):
    __tablename__ = "realtime_events"

    id: int | None = Field(default=None, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    event: str = Field(max_length=80, index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=jsonb_column())
    created_at: datetime = Field(default_factory=utc_now, index=True)


class ProjectFolderBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    color: str | None = Field(default=None, max_length=32)
    position: int = 0


class ProjectFolderCreate(ProjectFolderBase):
    parent_id: UUID | None = None


class ProjectFolderUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: UUID | None = None
    color: str | None = Field(default=None, max_length=32)
    position: int | None = None


class ProjectFolder(ProjectFolderBase, table=True):
    __tablename__ = "project_folders"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    parent_id: UUID | None = Field(default=None, foreign_key="project_folders.id", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectFolderPublic(ProjectFolderBase):
    id: UUID
    project_id: UUID
    parent_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ProjectResourceBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: ResourceType
    resource_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=jsonb_column("metadata"),
    )
    thumbnail_url: str | None = None
    color: str | None = Field(default=None, max_length=32)
    position: int = 0


class ProjectResourceCreate(ProjectResourceBase):
    folder_id: UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ProjectResourceUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    folder_id: UUID | None = None
    resource_metadata: dict[str, Any] | None = None
    thumbnail_url: str | None = None
    color: str | None = Field(default=None, max_length=32)
    position: int | None = None


class ProjectResource(ProjectResourceBase, table=True):
    __tablename__ = "project_resources"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    folder_id: UUID | None = Field(default=None, foreign_key="project_folders.id", index=True)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=jsonb_column())
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    archived_at: datetime | None = None


class ProjectResourcePublic(ProjectResourceBase):
    id: UUID
    project_id: UUID
    folder_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ProjectResourceDetail(ProjectResourcePublic):
    data: dict[str, Any]


class ResourceRevision(SQLModel, table=True):
    __tablename__ = "resource_revisions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    resource_id: UUID = Field(foreign_key="project_resources.id", index=True)
    revision_number: int
    label: str | None = None
    data: dict[str, Any] = Field(default_factory=dict, sa_column=jsonb_column())
    resource_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=jsonb_column("metadata"),
    )
    created_at: datetime = Field(default_factory=utc_now)
    created_by: UUID | None = Field(default=None, foreign_key="users.id")
    is_autosave: bool = False


class ResourceExport(SQLModel, table=True):
    __tablename__ = "resource_exports"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    resource_id: UUID = Field(foreign_key="project_resources.id", index=True)
    kind: ExportKind
    file_url: str
    mime_type: str | None = None
    size_bytes: int | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ProjectTree(SQLModel):
    folders: list[ProjectFolderPublic]
    resources: list[ProjectResourcePublic]


class WorkspaceBootstrap(SQLModel):
    user: UserPublic
    projects: list[ProjectPublic]
