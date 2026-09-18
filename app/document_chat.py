"""Authenticated, durable document messages with a transactional realtime outbox."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ConfigDict, StrictInt, field_validator, model_validator
from sqlalchemy import and_, case, func, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Field, Session, SQLModel, select

from app.crud import get_project_with_access_or_404, list_project_user_ids
from app.models import (
    DocumentChatMessage,
    DocumentChatReadState,
    Project,
    ProjectMember,
    ProjectResource,
    RealtimeEventLog,
    User,
)
from app.realtime import realtime_broker

CHAT_MESSAGE_EVENT = "document.chat.created"
CHAT_READ_EVENT = "document.chat.read"
# Stable identifiers only: the client resolves these to the bundled Tiny RPG art.
# Never accept attachment URLs or paths from chat messages.
TINY_RPG_STICKER_IDS = frozenset({
    "tiny-rpg-neutral",
    "tiny-rpg-angry",
    "tiny-rpg-sad",
    "tiny-rpg-laughing",
    "tiny-rpg-eager",
    "tiny-rpg-shocked",
    "tiny-rpg-speechless",
    "tiny-rpg-sleepy",
    "tiny-rpg-quiet",
    "tiny-rpg-dizzy",
    "tiny-rpg-love",
    "tiny-rpg-surprised",
    "tiny-rpg-confused",
    "tiny-rpg-thinking",
    "tiny-rpg-idea",
    "tiny-rpg-lit",
    "tiny-rpg-inviting",
    "tiny-rpg-pointing-up",
    "tiny-rpg-pointing-down",
    "tiny-rpg-pointing-left",
    "tiny-rpg-pointing-right",
    "tiny-rpg-drooling",
    "tiny-rpg-kissing",
    "tiny-rpg-no",
    "tiny-rpg-yes",
    "tiny-rpg-tongue-out",
    "tiny-rpg-prohibited",
    "tiny-rpg-headblown",
    "tiny-rpg-adorable",
    "tiny-rpg-thumbs-up",
    "tiny-rpg-sweating",
    "tiny-rpg-frustrated",
})
logger = logging.getLogger(__name__)


class DocumentChatMessageCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: UUID
    body: str = Field(default="", max_length=2000)
    sticker_id: str | None = Field(default=None, max_length=80)

    @field_validator("body", mode="before")
    @classmethod
    def normalize_body(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Message body must be text")
        return value.strip()

    @field_validator("sticker_id", mode="before")
    @classmethod
    def validate_sticker_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or value not in TINY_RPG_STICKER_IDS:
            raise ValueError("Choose a sticker from the Tiny RPG pack")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> "DocumentChatMessageCreate":
        if self.body and self.sticker_id is not None:
            raise ValueError("Send either message text or a sticker, not both")
        if not self.body and self.sticker_id is None:
            raise ValueError("A message requires text or a sticker")
        return self


class DocumentChatAuthorPublic(SQLModel):
    id: UUID
    username: str | None = None
    display_name: str
    avatar_url: str | None = None
    avatar_pixel_art: dict[str, Any] | None = None


class DocumentChatMessagePublic(SQLModel):
    id: int
    client_message_id: UUID
    project_id: UUID
    resource_id: UUID
    author: DocumentChatAuthorPublic
    body: str
    sticker_id: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DocumentChatMessagesPublic(SQLModel):
    messages: list[DocumentChatMessagePublic]
    has_more: bool
    next_before_id: int | None
    unread_count: int = 0
    last_read_message_id: int = 0
    last_message_id: int | None = None
    history_visible_from: datetime


class DocumentChatReadUpdate(SQLModel):
    model_config = ConfigDict(extra="forbid")

    last_read_message_id: StrictInt = Field(ge=0)


class DocumentChatUnreadPublic(SQLModel):
    resource_id: UUID
    unread_count: int
    last_message_id: int | None
    last_read_message_id: int


class ProjectChatUnreadPublic(SQLModel):
    documents: list[DocumentChatUnreadPublic]


def aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def chat_history_visible_from(*, session: Session, user_id: UUID, project_id: UUID) -> datetime:
    """Call under the project access fence; membership dates survive owner transfers."""
    joined_at = session.exec(
        select(ProjectMember.created_at).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    ).first()
    if joined_at is not None:
        return aware_utc(joined_at)
    owner_dates = session.exec(
        select(Project.created_at, Project.owner_joined_at)
        .where(Project.id == project_id, Project.owner_id == user_id)
    ).first()
    if owner_dates is None:
        raise HTTPException(status_code=404, detail="Project not found")
    created_at, owner_joined_at = owner_dates
    return aware_utc(owner_joined_at or created_at)


def _unread_documents(
    *, session: Session, user_id: UUID, project_id: UUID, joined_at: datetime,
    resource_id: UUID | None = None,
) -> list[DocumentChatUnreadPublic]:
    # Select identities and aggregate columns only. This query never loads a canvas
    # or issues a query per document. A stale membership epoch cannot suppress unread.
    cursor = func.coalesce(DocumentChatReadState.last_read_message_id, 0)
    statement = (
        select(
            ProjectResource.id,
            cursor.label("last_read_message_id"),
            func.max(DocumentChatMessage.id).label("last_message_id"),
            func.coalesce(func.sum(case((and_(
                DocumentChatMessage.id > cursor,
                DocumentChatMessage.author_id != user_id,
            ), 1), else_=0)), 0).label("unread_count"),
        )
        .outerjoin(DocumentChatReadState, and_(
            DocumentChatReadState.resource_id == ProjectResource.id,
            DocumentChatReadState.user_id == user_id,
            DocumentChatReadState.joined_at == joined_at,
        ))
        .outerjoin(DocumentChatMessage, and_(
            DocumentChatMessage.resource_id == ProjectResource.id,
            DocumentChatMessage.created_at >= joined_at,
        ))
        .where(ProjectResource.project_id == project_id)
        .group_by(ProjectResource.id, DocumentChatReadState.last_read_message_id)
        .order_by(ProjectResource.id)
    )
    if resource_id is not None:
        statement = statement.where(ProjectResource.id == resource_id)
    return [DocumentChatUnreadPublic(
        resource_id=resource, last_read_message_id=int(read_id),
        last_message_id=last_id, unread_count=int(unread),
    ) for resource, read_id, last_id, unread in session.exec(statement).all()]


def list_project_chat_unread(
    *, session: Session, user_id: UUID, project_id: str,
) -> ProjectChatUnreadPublic:
    project, _role = get_project_with_access_or_404(
        session=session, user_id=user_id, project_id=project_id
    )
    joined_at = chat_history_visible_from(session=session, user_id=user_id, project_id=project.id)
    return ProjectChatUnreadPublic(documents=_unread_documents(
        session=session, user_id=user_id, project_id=project.id, joined_at=joined_at,
    ))


def mark_document_chat_read(
    *, session: Session, user_id: UUID, project_id: str, resource_id: str,
    read_in: DocumentChatReadUpdate,
) -> DocumentChatUnreadPublic:
    project, resource = chat_resource_with_access(
        session=session, user_id=user_id, project_id=project_id, resource_id=resource_id,
    )
    joined_at = chat_history_visible_from(session=session, user_id=user_id, project_id=project)
    requested = read_in.last_read_message_id
    if requested and session.exec(select(DocumentChatMessage.id).where(
        DocumentChatMessage.id == requested,
        DocumentChatMessage.project_id == project,
        DocumentChatMessage.resource_id == resource,
        DocumentChatMessage.created_at >= joined_at,
    )).first() is None:
        raise HTTPException(status_code=422, detail="Reading position must be a visible message")
    # A native upsert serializes concurrent devices on this row, including the
    # first insert, without a read/modify/write race or resetting a newer cursor.
    insert = postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(DocumentChatReadState).values(
        user_id=user_id, project_id=project, resource_id=resource, joined_at=joined_at,
        last_read_message_id=requested, updated_at=datetime.now(UTC),
    )
    same_epoch = DocumentChatReadState.joined_at == statement.excluded.joined_at
    advanced = case(
        (DocumentChatReadState.last_read_message_id > statement.excluded.last_read_message_id,
         DocumentChatReadState.last_read_message_id),
        else_=statement.excluded.last_read_message_id,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[DocumentChatReadState.user_id, DocumentChatReadState.resource_id],
        set_={
            "joined_at": statement.excluded.joined_at,
            "last_read_message_id": case((same_epoch, advanced),
                                         else_=statement.excluded.last_read_message_id),
            "updated_at": statement.excluded.updated_at,
        },
    )
    try:
        session.execute(statement)
        result = _unread_documents(
            session=session, user_id=user_id, project_id=project, joined_at=joined_at,
            resource_id=resource,
        )[0]
        data = {
            "project_id": str(project), **result.model_dump(mode="json"),
            "history_visible_from": joined_at.isoformat(),
        }
        session.add(RealtimeEventLog(user_id=user_id, event=CHAT_READ_EVENT, data=data))
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    try:
        realtime_broker.publish(user_ids={user_id}, event=CHAT_READ_EVENT, data=data)
    except Exception:
        logger.warning("Realtime delivery failed for committed chat read state", exc_info=True)
    return result


def message_to_public(
    *, message: DocumentChatMessage, author: User
) -> DocumentChatMessagePublic:
    assert message.id is not None
    return DocumentChatMessagePublic(
        id=message.id,
        client_message_id=message.client_message_id,
        project_id=message.project_id,
        resource_id=message.resource_id,
        author=DocumentChatAuthorPublic(
            id=author.id,
            username=author.username,
            display_name=(
                author.username
                if author.username is not None and author.username.strip()
                else str(author.email)
            ),
            avatar_url=author.avatar_url,
            avatar_pixel_art=author.avatar_pixel_art,
        ),
        body=message.body,
        sticker_id=message.sticker_id,
        created_at=message.created_at,
    )


def chat_resource_with_access(
    *, session: Session, user_id: UUID, project_id: str, resource_id: str, writing: bool = False
) -> tuple[UUID, UUID]:
    # The project access fence remains held through the message/outbox commit.
    # Access mutations take the exclusive project fence before changing membership.
    project, _role = get_project_with_access_or_404(
        session=session, user_id=user_id, project_id=project_id
    )
    try:
        parsed_resource_id = UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found") from None
    resource = session.exec(
        select(ProjectResource.id)
        .where(ProjectResource.id == parsed_resource_id, ProjectResource.project_id == project.id)
        # KEY SHARE prevents deletion and does not conflict with normal non-key
        # UPDATEs. Existing image operations explicitly use FOR UPDATE, so those
        # transactions can briefly contend; no bitmap is selected by this query.
        # Select only the identity: chatting must never deserialize a large canvas.
        .with_for_update(read=True, key_share=True)
    ).first()
    if resource is None:
        raise HTTPException(status_code=404, detail="Resource not found")
    if writing and session.get_bind().dialect.name == "postgresql":
        # Serialize only this conversation. IDs must commit in order for after_id
        # catch-up, and concurrent retries must see the already committed receipt.
        key = (resource.int ^ (resource.int >> 64)) & ((1 << 64) - 1)
        if key >= 1 << 63:
            key -= 1 << 64
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    return project.id, resource


def list_document_chat_messages(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_id: str,
    limit: int = 50,
    before_id: int | None = None,
    after_id: int | None = None,
) -> DocumentChatMessagesPublic:
    if before_id is not None and after_id is not None:
        raise HTTPException(status_code=422, detail="Use before_id or after_id, not both")
    project, resource = chat_resource_with_access(
        session=session, user_id=user_id, project_id=project_id, resource_id=resource_id
    )
    joined_at = chat_history_visible_from(session=session, user_id=user_id, project_id=project)
    statement = (
        select(DocumentChatMessage, User)
        .join(User, User.id == DocumentChatMessage.author_id)
        .where(
            DocumentChatMessage.resource_id == resource,
            DocumentChatMessage.created_at >= joined_at,
        )
    )
    if before_id is not None:
        statement = statement.where(DocumentChatMessage.id < before_id)
    if after_id is not None:
        statement = statement.where(DocumentChatMessage.id > after_id).order_by(
            DocumentChatMessage.id
        )
    else:
        statement = statement.order_by(DocumentChatMessage.id.desc())
    rows = list(session.exec(statement.limit(limit + 1)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after_id is None:
        rows.reverse()
    messages = [message_to_public(message=message, author=author) for message, author in rows]
    unread = _unread_documents(
        session=session, user_id=user_id, project_id=project, joined_at=joined_at,
        resource_id=resource,
    )[0]
    return DocumentChatMessagesPublic(
        messages=messages,
        has_more=has_more,
        next_before_id=messages[0].id if messages else None,
        unread_count=unread.unread_count,
        last_read_message_id=unread.last_read_message_id,
        last_message_id=unread.last_message_id,
        history_visible_from=joined_at,
    )


def create_document_chat_message(
    *,
    session: Session,
    author: User,
    project_id: str,
    resource_id: str,
    message_in: DocumentChatMessageCreate,
) -> DocumentChatMessagePublic:
    project, resource = chat_resource_with_access(
        session=session,
        user_id=author.id,
        project_id=project_id,
        resource_id=resource_id,
        writing=True,
    )
    joined_at = chat_history_visible_from(session=session, user_id=author.id, project_id=project)
    receipt_statement = select(DocumentChatMessage).where(
        DocumentChatMessage.resource_id == resource,
        DocumentChatMessage.author_id == author.id,
        DocumentChatMessage.client_message_id == message_in.client_message_id,
    )

    def duplicate_response(message: DocumentChatMessage) -> DocumentChatMessagePublic:
        if aware_utc(message.created_at) < joined_at:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "chat_message_before_membership",
                    "message": "This message identifier belongs to a previous project membership",
                },
            )
        if message.body != message_in.body or message.sticker_id != message_in.sticker_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "chat_message_id_reused",
                    "message": "This message identifier was already used for different content",
                },
            )
        result = message_to_public(message=message, author=author)
        session.commit()  # Release the access fence even on an acknowledged retry.
        return result

    existing = session.exec(receipt_statement).first()
    if existing is not None:
        return duplicate_response(existing)
    message = DocumentChatMessage(
        project_id=project,
        resource_id=resource,
        author_id=author.id,
        client_message_id=message_in.client_message_id,
        body=message_in.body,
        sticker_id=message_in.sticker_id,
    )
    try:
        session.add(message)
        session.flush()
        result = message_to_public(message=message, author=author)
        data = {
            "project_id": str(project),
            "resource_id": str(resource),
            "message": result.model_dump(mode="json"),
        }
        recipients = list_project_user_ids(session=session, project_id=project)
        # A successful HTTP acknowledgement guarantees both the message and every
        # recipient's reconnect event were persisted in this same transaction.
        session.add_all(
            RealtimeEventLog(user_id=user_id, event=CHAT_MESSAGE_EVENT, data=data)
            for user_id in recipients
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        # SQLite/test races and retry-after-lost-ACK must revalidate access after
        # rollback; permissions may have changed when the fence was released.
        chat_resource_with_access(
            session=session,
            user_id=author.id,
            project_id=project_id,
            resource_id=resource_id,
            writing=True,
        )
        joined_at = chat_history_visible_from(
            session=session, user_id=author.id, project_id=project,
        )
        existing = session.exec(receipt_statement).first()
        if existing is None:
            raise
        return duplicate_response(existing)
    except SQLAlchemyError:
        session.rollback()
        raise
    try:
        realtime_broker.publish(user_ids=recipients, event=CHAT_MESSAGE_EVENT, data=data)
    except Exception:
        # Realtime is best effort after the durable message/outbox commit. A
        # disconnected subscriber must not turn an accepted message into an
        # HTTP failure; reconnect catch-up delivers the persisted event.
        logger.warning(
            "Realtime delivery failed for committed document chat message", exc_info=True
        )
    return result
