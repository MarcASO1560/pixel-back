"""Authenticated, durable document messages with a transactional realtime outbox."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Field, Session, SQLModel, select

from app.crud import get_project_with_access_or_404, list_project_user_ids
from app.models import DocumentChatMessage, ProjectResource, RealtimeEventLog, User
from app.realtime import realtime_broker

CHAT_MESSAGE_EVENT = "document.chat.created"
logger = logging.getLogger(__name__)


class DocumentChatMessageCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: UUID
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body", mode="before")
    @classmethod
    def normalize_body(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Message body must be text")
        return value.strip()


class DocumentChatAuthorPublic(SQLModel):
    id: UUID
    username: str | None = None
    avatar_url: str | None = None
    avatar_pixel_art: dict[str, Any] | None = None


class DocumentChatMessagePublic(SQLModel):
    id: int
    client_message_id: UUID
    project_id: UUID
    resource_id: UUID
    author: DocumentChatAuthorPublic
    body: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DocumentChatMessagesPublic(SQLModel):
    messages: list[DocumentChatMessagePublic]
    has_more: bool
    next_before_id: int | None


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
            avatar_url=author.avatar_url,
            avatar_pixel_art=author.avatar_pixel_art,
        ),
        body=message.body,
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
    _project, resource = chat_resource_with_access(
        session=session, user_id=user_id, project_id=project_id, resource_id=resource_id
    )
    statement = (
        select(DocumentChatMessage, User)
        .join(User, User.id == DocumentChatMessage.author_id)
        .where(DocumentChatMessage.resource_id == resource)
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
    return DocumentChatMessagesPublic(
        messages=messages,
        has_more=has_more,
        next_before_id=messages[0].id if messages else None,
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
    receipt_statement = select(DocumentChatMessage).where(
        DocumentChatMessage.resource_id == resource,
        DocumentChatMessage.author_id == author.id,
        DocumentChatMessage.client_message_id == message_in.client_message_id,
    )

    def duplicate_response(message: DocumentChatMessage) -> DocumentChatMessagePublic:
        if message.body != message_in.body:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "chat_message_id_reused",
                    "message": "This message identifier was already used for different text",
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
