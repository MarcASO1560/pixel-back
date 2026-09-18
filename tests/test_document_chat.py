"""Conversation isolation, durable delivery, pagination, and retry safety."""

from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select
from test_image_operations import image_client as image_client

from app import document_chat
from app.document_chat import chat_resource_with_access
from app.models import (
    DocumentChatMessage,
    Project,
    ProjectMember,
    ProjectResource,
    RealtimeEventLog,
)


def chat_url(image):
    return image["url"] + "/chat/messages"


def send(image, body="Hola", role="owner", client_message_id=None, url=None):
    return image["client"].post(
        url or chat_url(image),
        json={"client_message_id": str(client_message_id or uuid4()), "body": body},
        headers=image["headers"][role],
    )


def read(image, role="owner", **query):
    return image["client"].get(chat_url(image), headers=image["headers"][role], params=query)


def test_members_including_viewers_can_converse_without_changing_the_drawing(image_client):
    image = image_client
    before = deepcopy(image["resource"].data)
    revision = image["resource"].revision
    timestamp = image["resource"].updated_at
    messages = []
    for role in ("owner", "editor", "viewer"):
        response = send(image, f"  Desde {role}  ", role)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        message = response.json()
        assert message["body"] == f"Desde {role}"
        assert message["created_at"].endswith("Z")
        assert message["project_id"] == str(image["project"].id)
        assert message["resource_id"] == str(image["resource"].id)
        assert set(message["author"]) == {"id", "username", "avatar_url", "avatar_pixel_art"}
        assert "email" not in str(message)
        messages.append(message)
    assert read(image, "viewer").json()["messages"] == messages
    image["session"].refresh(image["resource"])
    assert image["resource"].revision == revision
    assert image["resource"].data == before
    assert image["resource"].updated_at == timestamp


def test_author_avatar_and_username_are_public_but_email_is_not(image_client):
    image = image_client
    image["editor"].username = "artist"
    image["editor"].avatar_url = "https://example.com/avatar.png"
    image["editor"].avatar_pixel_art = {"version": 1, "pixels": ["#FFFFFF"]}
    image["session"].add(image["editor"])
    image["session"].commit()
    author = send(image, role="editor").json()["author"]
    assert author == {
        "id": str(image["editor"].id),
        "username": "artist",
        "avatar_url": "https://example.com/avatar.png",
        "avatar_pixel_art": {"version": 1, "pixels": ["#FFFFFF"]},
    }


def test_outsiders_and_unauthenticated_requests_cannot_read_or_append(image_client):
    image = image_client
    assert send(image, role="outsider").status_code == 404
    assert read(image, "outsider").status_code == 404
    assert image["client"].get(chat_url(image)).status_code in (401, 403)
    assert image["client"].post(chat_url(image), json={}).status_code in (401, 403)


def test_cross_project_and_missing_resources_are_rejected(image_client):
    image = image_client
    other_project = Project(name="Other", owner_id=image["project"].owner_id)
    image["session"].add(other_project)
    image["session"].commit()
    for project_id, resource_id in (
        (other_project.id, image["resource"].id),
        (image["project"].id, uuid4()),
        ("invalid", image["resource"].id),
        (image["project"].id, "invalid"),
    ):
        url = f"/api/v1/projects/{project_id}/resources/{resource_id}/chat/messages"
        assert send(image, url=url).status_code == 404
        assert image["client"].get(url, headers=image["headers"]["owner"]).status_code == 404


@pytest.mark.parametrize("change", ["block", "remove"])
def test_revoked_members_cannot_read_retry_or_append(image_client, change):
    image = image_client
    client_id = uuid4()
    assert send(image, role="editor", client_message_id=client_id).status_code == 200
    project_url = f"/api/v1/projects/{image['project'].id}"
    if change == "block":
        response = image["client"].post(
            project_url + f"/blocked-users/{image['editor'].id}",
            headers=image["headers"]["owner"],
        )
        denied_status = 403
    else:
        response = image["client"].delete(
            project_url + f"/members/{image['editor'].id}",
            headers=image["headers"]["owner"],
        )
        denied_status = 404
    assert response.status_code in (200, 204)
    assert read(image, "editor").status_code == denied_status
    assert send(image, role="editor").status_code == denied_status
    assert send(image, role="editor", client_message_id=client_id).status_code == denied_status


@pytest.mark.parametrize("body", ["", "  \n\t  ", "a" * 2001, None, 7, {}])
def test_message_validation(image_client, body):
    assert send(image_client, body).status_code == 422


def test_body_limit_multiline_unicode_and_plaintext(image_client):
    assert send(image_client, "a" * 2000).status_code == 200
    value = "<script>alert('x')</script>\n😀 Buenos días"
    assert send(image_client, value).json()["body"] == value
    malformed = image_client["client"].post(
        chat_url(image_client),
        json={"client_message_id": "not-a-uuid", "body": "hello"},
        headers=image_client["headers"]["owner"],
    )
    assert malformed.status_code == 422


def test_retries_acknowledge_once_and_reused_id_with_changed_text_is_a_conflict(image_client):
    image = image_client
    client_id = uuid4()
    first = send(image, "hello", client_message_id=client_id)
    assert send(image, " hello ", client_message_id=client_id).json() == first.json()
    assert send(image, "changed", client_message_id=client_id).status_code == 409
    assert len(read(image).json()["messages"]) == 1
    events = (
        image["session"]
        .exec(
            select(RealtimeEventLog).where(
                RealtimeEventLog.event == document_chat.CHAT_MESSAGE_EVENT
            )
        )
        .all()
    )
    assert len(events) == 3
    # Client identifiers are scoped to author and document, not global UUID ownership.
    assert send(image, "other author", "editor", client_id).status_code == 200
    other = ProjectResource(
        project_id=image["project"].id, name="Other document", type="text", data={}
    )
    image["session"].add(other)
    image["session"].commit()
    url = f"/api/v1/projects/{image['project'].id}/resources/{other.id}/chat/messages"
    assert send(image, "other document", client_message_id=client_id, url=url).status_code == 200
    assert len(read(image).json()["messages"]) == 2


def test_transactional_event_is_sent_to_all_current_members_including_author(
    image_client, monkeypatch
):
    image = image_client
    publish = Mock()
    monkeypatch.setattr(document_chat.realtime_broker, "publish", publish)
    response = send(image)
    message = response.json()
    assert response.status_code == 200
    events = image["session"].exec(select(RealtimeEventLog)).all()
    assert len(events) == 3
    recipients = {event.user_id for event in events}
    assert recipients == {
        image["project"].owner_id,
        *image["session"]
        .exec(select(ProjectMember.user_id).where(ProjectMember.project_id == image["project"].id))
        .all(),
    }
    data = {
        "project_id": str(image["project"].id),
        "resource_id": str(image["resource"].id),
        "message": message,
    }
    assert all(
        event.event == document_chat.CHAT_MESSAGE_EVENT and event.data == data for event in events
    )
    publish.assert_called_once_with(
        user_ids=recipients, event=document_chat.CHAT_MESSAGE_EVENT, data=data
    )
    send(image, client_message_id=message["client_message_id"])
    assert publish.call_count == 1


def test_failed_outbox_commit_does_not_acknowledge_or_publish(image_client, monkeypatch):
    image = image_client
    publish = Mock()
    monkeypatch.setattr(document_chat.realtime_broker, "publish", publish)
    original_commit = image["session"].commit
    monkeypatch.setattr(
        image["session"], "commit", Mock(side_effect=SQLAlchemyError("outbox failed"))
    )
    with pytest.raises(SQLAlchemyError, match="outbox failed"):
        send(image)
    monkeypatch.setattr(image["session"], "commit", original_commit)
    assert image["session"].exec(select(DocumentChatMessage)).all() == []
    assert image["session"].exec(select(RealtimeEventLog)).all() == []
    publish.assert_not_called()


def test_broker_failure_still_acknowledges_committed_message_and_outbox(image_client, monkeypatch):
    image = image_client
    publish = Mock(side_effect=RuntimeError("subscriber closed"))
    monkeypatch.setattr(document_chat.realtime_broker, "publish", publish)
    client_id = uuid4()
    response = send(image, client_message_id=client_id)
    assert response.status_code == 200
    assert read(image).json()["messages"] == [response.json()]
    assert len(image["session"].exec(select(RealtimeEventLog)).all()) == 3
    assert send(image, client_message_id=client_id).json() == response.json()
    publish.assert_called_once()


def test_latest_history_and_forward_catchup_have_stable_ascending_order(image_client):
    image = image_client
    messages = [send(image, f"message {i}").json() for i in range(7)]
    latest = read(image, limit=3).json()
    assert latest["messages"] == messages[-3:]
    assert latest["has_more"] is True
    assert latest["next_before_id"] == messages[-3]["id"]
    older = read(image, limit=3, before_id=latest["next_before_id"]).json()
    assert older["messages"] == messages[1:4]
    assert older["has_more"] is True
    oldest = read(image, limit=3, before_id=older["next_before_id"]).json()
    assert oldest["messages"] == messages[:1]
    assert oldest["has_more"] is False
    forward = read(image, limit=3, after_id=0).json()
    assert forward["messages"] == messages[:3]
    assert forward["has_more"] is True
    forward2 = read(image, limit=3, after_id=messages[2]["id"]).json()
    assert forward2["messages"] == messages[3:6]
    assert forward2["has_more"] is True
    last = read(image, after_id=messages[-1]["id"]).json()
    assert last == {"messages": [], "has_more": False, "next_before_id": None}


@pytest.mark.parametrize(
    "query",
    [
        {"before_id": 1, "after_id": 0},
        {"limit": 0},
        {"limit": 101},
        {"before_id": 0},
        {"after_id": -1},
    ],
)
def test_pagination_validation(image_client, query):
    assert read(image_client, **query).status_code == 422


@pytest.mark.parametrize("target", ["resource", "project", "author"])
def test_foreign_key_cleanup_when_document_project_or_author_is_deleted(image_client, target):
    image = image_client
    assert send(image, role="editor").status_code == 200
    # Existing fixture intentionally does not enforce FKs for older model tests.
    # Enable real cascade behavior for this schema-specific regression.
    image["session"].commit()
    with image["session"].get_bind().connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    if target == "resource":
        image["session"].exec(
            delete(ProjectResource).where(ProjectResource.id == image["resource"].id)
        )
    elif target == "project":
        image["session"].exec(delete(RealtimeEventLog))
        image["session"].exec(delete(ProjectMember))
        image["session"].exec(
            delete(ProjectResource).where(ProjectResource.project_id == image["project"].id)
        )
        image["session"].exec(delete(Project).where(Project.id == image["project"].id))
    else:
        image["session"].exec(
            delete(RealtimeEventLog).where(RealtimeEventLog.user_id == image["editor"].id)
        )
        image["session"].exec(
            delete(ProjectMember).where(ProjectMember.user_id == image["editor"].id)
        )
        image["session"].delete(image["editor"])
    image["session"].commit()
    assert image["session"].exec(select(DocumentChatMessage)).all() == []


def test_chat_identity_query_never_selects_bitmap_and_uses_key_share(image_client, monkeypatch):
    from sqlalchemy.dialects import postgresql

    session = image_client["session"]
    statements = []
    original_exec = session.exec

    def record(statement, *args, **kwargs):
        statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", record)
    chat_resource_with_access(
        session=session,
        user_id=image_client["project"].owner_id,
        project_id=str(image_client["project"].id),
        resource_id=str(image_client["resource"].id),
    )
    identity_query = next(value for value in statements if "FROM project_resources" in value)
    assert "FOR KEY SHARE" in identity_query
    assert identity_query.startswith("SELECT project_resources.id")
    assert "project_resources.data" not in identity_query
