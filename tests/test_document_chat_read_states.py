"""Persistent, private unread positions shared by every document type and device."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select
from test_document_chat import read, send
from test_image_operations import image_client as image_client

from app import document_chat
from app.document_chat import DocumentChatReadUpdate, mark_document_chat_read
from app.models import DocumentChatMessage, DocumentChatReadState, ProjectMember, ProjectResource


def summary(image, role="editor"):
    return image["client"].get(
        f"/api/v1/projects/{image['project'].id}/chat/unread", headers=image["headers"][role],
    )


def mark_read(image, message_id, role="editor", resource_id=None):
    return image["client"].post(
        f"/api/v1/projects/{image['project'].id}/resources/"
        f"{resource_id or image['resource'].id}/chat/read",
        headers=image["headers"][role], json={"last_read_message_id": message_id},
    )


def test_summary_counts_only_visible_other_authors_and_get_never_marks_read(image_client):
    image = image_client
    first = send(image, "From owner").json()
    send(image, "My own message", role="editor")
    latest = send(image, "From viewer", role="viewer").json()
    response = summary(image)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"documents": [{
        "resource_id": str(image["resource"].id), "unread_count": 2,
        "last_message_id": latest["id"], "last_read_message_id": 0,
    }]}
    page = read(image, "editor", limit=1).json()
    assert page["unread_count"] == 2
    assert page["last_read_message_id"] == 0
    assert page["last_message_id"] == latest["id"]
    assert page["history_visible_from"].endswith("Z")
    assert summary(image).json() == response.json()
    assert mark_read(image, first["id"]).json()["unread_count"] == 1


def test_read_cursor_is_monotonic_idempotent_and_persists_across_sessions(image_client):
    image = image_client
    first = send(image).json()
    second = send(image, "Next").json()
    advanced = mark_read(image, second["id"])
    assert advanced.status_code == 200
    assert advanced.headers["cache-control"] == "no-store"
    assert advanced.json()["last_read_message_id"] == second["id"]
    assert advanced.json()["unread_count"] == 0
    assert mark_read(image, first["id"]).json() == advanced.json()
    assert mark_read(image, second["id"]).json() == advanced.json()
    assert mark_read(image, 0).json() == advanced.json()
    assert summary(image).json()["documents"] == [advanced.json()]
    with Session(image["session"].get_bind()) as another_device:
        result = mark_document_chat_read(
            session=another_device, user_id=image["editor"].id,
            project_id=str(image["project"].id), resource_id=str(image["resource"].id),
            read_in=DocumentChatReadUpdate(last_read_message_id=first["id"]),
        )
    assert result.last_read_message_id == second["id"]
    assert result.unread_count == 0
    assert read(image, "editor").json()["last_read_message_id"] == second["id"]


def test_read_does_not_swallow_message_arriving_after_viewed_cursor(image_client):
    image = image_client
    visible = send(image).json()
    later = send(image, "Arrived after viewing").json()
    result = mark_read(image, visible["id"]).json()
    assert result["last_read_message_id"] == visible["id"]
    assert result["last_message_id"] == later["id"]
    assert result["unread_count"] == 1


@pytest.mark.parametrize("invalid", [True, False, "1", None, 1.5])
def test_read_position_requires_an_integer_message_id(image_client, invalid):
    assert mark_read(image_client, invalid).status_code == 422


@pytest.mark.parametrize("invalid", ["future", "other_document", "before_join", "negative"])
def test_read_rejects_positions_not_visible_in_this_document(image_client, invalid):
    image = image_client
    message = send(image).json()
    candidate = message["id"]
    if invalid == "future":
        candidate += 99
    elif invalid == "negative":
        candidate = -1
    elif invalid == "other_document":
        other = ProjectResource(project_id=image["project"].id, name="Other", type="text")
        image["session"].add(other)
        image["session"].commit()
        url = f"/api/v1/projects/{image['project'].id}/resources/{other.id}/chat/messages"
        candidate = send(image, url=url).json()["id"]
    else:
        member = image["session"].get(ProjectMember, (image["project"].id, image["editor"].id))
        member.created_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
        image["session"].add(member)
        image["session"].commit()
    assert mark_read(image, candidate).status_code == 422
    assert image["session"].exec(select(DocumentChatReadState)).all() == []


@pytest.mark.parametrize("role,status", [("outsider", 404), ("editor", 403)])
def test_unread_and_read_do_not_bypass_project_access(image_client, role, status):
    image = image_client
    message = send(image).json()
    if role == "editor":
        assert image["client"].post(
            f"/api/v1/projects/{image['project'].id}/blocked-users/{image['editor'].id}",
            headers=image["headers"]["owner"],
        ).status_code == 200
    assert summary(image, role).status_code == status
    assert mark_read(image, message["id"], role).status_code == status


def test_summary_aggregates_all_documents_without_loading_canvases_or_n_plus_one(
    image_client, monkeypatch,
):
    image = image_client
    resources = [ProjectResource(
        project_id=image["project"].id, name=f"Document {i}", type="text",
    ) for i in range(12)]
    image["session"].add_all(resources)
    image["session"].commit()
    statements = []
    original_exec = image["session"].exec

    def record(statement, *args, **kwargs):
        statements.append(str(statement))
        return original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(image["session"], "exec", record)
    response = summary(image)
    assert response.status_code == 200
    assert len(response.json()["documents"]) == 13
    queries = [value for value in statements if "FROM project_resources" in value]
    assert len(queries) == 1
    assert "project_resources.data" not in queries[0]
    assert "GROUP BY" in queries[0]
    assert all(row["unread_count"] == 0 for row in response.json()["documents"])


@pytest.mark.parametrize("resource_type", [
    "pixel_art", "pixel_animation", "text", "music_track", "sound_effect", "tileset",
])
def test_every_resource_type_shares_messages_privacy_and_reading_positions(
    image_client, resource_type,
):
    image = image_client
    image["resource"].type = resource_type
    image["session"].add(image["resource"])
    image["session"].commit()
    latest = send(image).json()
    assert read(image, "editor").json()["unread_count"] == 1
    result = mark_read(image, latest["id"])
    assert result.status_code == 200
    assert result.json()["unread_count"] == 0
    assert summary(image).json()["documents"] == [result.json()]


def test_read_state_outbox_is_atomic_and_private_to_the_reader(image_client, monkeypatch):
    from app.models import RealtimeEventLog

    image = image_client
    message = send(image).json()
    publish = Mock()
    monkeypatch.setattr(document_chat.realtime_broker, "publish", publish)
    result = mark_read(image, message["id"])
    assert result.status_code == 200
    events = image["session"].exec(select(RealtimeEventLog).where(
        RealtimeEventLog.event == document_chat.CHAT_READ_EVENT,
    )).all()
    assert len(events) == 1
    assert events[0].user_id == image["editor"].id
    assert events[0].data["last_read_message_id"] == message["id"]
    assert publish.call_args.kwargs["user_ids"] == {image["editor"].id}
    original_commit = image["session"].commit
    monkeypatch.setattr(image["session"], "commit", Mock(side_effect=SQLAlchemyError("failed")))
    newer = DocumentChatMessage(
        project_id=image["project"].id, resource_id=image["resource"].id,
        author_id=image["project"].owner_id, client_message_id=uuid4(), body="Newer",
    )
    image["session"].add(newer)
    image["session"].flush()
    newer_id = newer.id
    monkeypatch.setattr(image["session"], "commit", original_commit)
    image["session"].commit()
    monkeypatch.setattr(image["session"], "commit", Mock(side_effect=SQLAlchemyError("failed")))
    with pytest.raises(SQLAlchemyError, match="failed"):
        mark_read(image, newer_id)
    monkeypatch.setattr(image["session"], "commit", original_commit)
    state = image["session"].exec(select(DocumentChatReadState)).one()
    assert state.last_read_message_id == message["id"]
    assert publish.call_count == 1


@pytest.mark.parametrize("transfer", ["leave", "demote"])
def test_owner_transfer_keeps_incoming_and_previous_owners_history_boundaries(
    image_client, transfer,
):
    image = image_client
    original_owner = image["project"].owner_id
    old = send(image, "Before editor joined").json()
    joined = datetime.now(UTC).replace(tzinfo=None)
    member = image["session"].get(ProjectMember, (image["project"].id, image["editor"].id))
    member.created_at = joined
    member.role = "owner"
    image["session"].add(member)
    image["session"].commit()
    recent = send(image, "After joining").json()
    assert mark_read(image, recent["id"]).json()["last_read_message_id"] == recent["id"]
    project_url = f"/api/v1/projects/{image['project'].id}"
    if transfer == "leave":
        result = image["client"].delete(
            project_url + "/members/me", headers=image["headers"]["owner"],
        )
        assert result.status_code == 204
    else:
        result = image["client"].patch(
            project_url + f"/members/{original_owner}", json={"role": "editor"},
            headers=image["headers"]["editor"],
        )
        assert result.status_code == 200
        # The previous original owner retains the full conversation after demotion.
        assert [row["id"] for row in read(image, "owner").json()["messages"]] == [
            old["id"], recent["id"],
        ]
    incoming = read(image, "editor").json()
    assert [row["id"] for row in incoming["messages"]] == [recent["id"]]
    assert incoming["history_visible_from"] == joined.replace(tzinfo=UTC).isoformat().replace(
        "+00:00", "Z",
    )
    assert incoming["last_read_message_id"] == recent["id"]
    assert incoming["unread_count"] == 0
    assert mark_read(image, old["id"]).status_code == 422
