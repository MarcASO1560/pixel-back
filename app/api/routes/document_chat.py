from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentUser, SessionDep
from app.document_chat import (
    DocumentChatMessageCreate,
    DocumentChatMessagePublic,
    DocumentChatMessagesPublic,
    DocumentChatReadUpdate,
    DocumentChatUnreadPublic,
    ProjectChatUnreadPublic,
    create_document_chat_message,
    list_document_chat_messages,
    list_project_chat_unread,
    mark_document_chat_read,
)

router = APIRouter()
CHAT_PATH = "/{project_id}/resources/{resource_id}/chat/messages"


@router.get("/{project_id}/chat/unread", response_model=ProjectChatUnreadPublic)
def read_project_chat_unread(
    response: Response, session: SessionDep, current_user: CurrentUser, project_id: str,
) -> ProjectChatUnreadPublic:
    response.headers["Cache-Control"] = "no-store"
    return list_project_chat_unread(
        session=session, user_id=current_user.id, project_id=project_id,
    )


@router.post("/{project_id}/resources/{resource_id}/chat/read",
             response_model=DocumentChatUnreadPublic)
def update_document_chat_read(
    response: Response, session: SessionDep, current_user: CurrentUser,
    project_id: str, resource_id: str, read_in: DocumentChatReadUpdate,
) -> DocumentChatUnreadPublic:
    response.headers["Cache-Control"] = "no-store"
    return mark_document_chat_read(
        session=session, user_id=current_user.id, project_id=project_id, resource_id=resource_id,
        read_in=read_in,
    )


@router.get(CHAT_PATH, response_model=DocumentChatMessagesPublic)
def read_document_chat_messages(
    response: Response,
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    after_id: Annotated[int | None, Query(ge=0)] = None,
) -> DocumentChatMessagesPublic:
    response.headers["Cache-Control"] = "no-store"
    return list_document_chat_messages(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


@router.post(CHAT_PATH, response_model=DocumentChatMessagePublic)
def append_document_chat_message(
    response: Response,
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
    message_in: DocumentChatMessageCreate,
) -> DocumentChatMessagePublic:
    response.headers["Cache-Control"] = "no-store"
    return create_document_chat_message(
        session=session,
        author=current_user,
        project_id=project_id,
        resource_id=resource_id,
        message_in=message_in,
    )
