"""Membership-epoch privacy for history, unread summaries, and durable replay."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select
from test_document_chat import read, send
from test_image_operations import image_client as image_client

from app.api.deps import ACCESS_TOKEN_COOKIE_NAME
from app.api.routes import events
from app.document_chat import aware_utc, message_to_public
from app.models import (
    DocumentChatMessage,
    DocumentChatReadState,
    ProjectBlockedUser,
    ProjectMember,
    RealtimeEventLog,
    User,
)
from app.realtime import can_deliver_realtime_event

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def set_initial_epoch(image, joined_at=BASE):
    image["project"].created_at = BASE
    image["session"].add(image["project"])
    for member in image["session"].exec(select(ProjectMember)).all():
        member.created_at = joined_at
        image["session"].add(member)
    image["session"].commit()


def editor_membership(image):
    return image["session"].get(ProjectMember, (image["project"].id, image["editor"].id))


def insert_message(image, body, created_at, author_id=None):
    message = DocumentChatMessage(
        project_id=image["project"].id,
        resource_id=image["resource"].id,
        author_id=author_id or image["project"].owner_id,
        client_message_id=uuid4(),
        body=body,
        created_at=created_at,
    )
    image["session"].add(message)
    image["session"].commit()
    return message


def insert_event(image, message, user_id=None, transform=None):
    author = image["session"].get(User, message.author_id)
    data = {
        "project_id": str(image["project"].id),
        "resource_id": str(image["resource"].id),
        "message": message_to_public(message=message, author=author).model_dump(mode="json"),
    }
    if transform:
        transform(data)
    row = RealtimeEventLog(
        user_id=user_id or image["editor"].id,
        event="document.chat.created",
        data=data,
    )
    image["session"].add(row)
    image["session"].commit()
    return row


def unread(image, role="editor"):
    return image["client"].get(
        f"/api/v1/projects/{image['project'].id}/chat/unread",
        headers=image["headers"][role],
    )


def mark_read(image, message_id, role="editor"):
    return image["client"].post(
        image["url"] + "/chat/read",
        headers=image["headers"][role],
        json={"last_read_message_id": message_id},
    )


def pending(image, role="editor", after=0):
    token = image["headers"][role]["Authorization"].split()[1]
    image["client"].cookies.set(ACCESS_TOKEN_COOKIE_NAME, token)
    return image["client"].get("/api/v1/events/pending", params={"after_event_id": after})


@pytest.mark.parametrize("member_role", ["viewer", "editor", "owner"])
def test_member_boundary_applies_to_both_pagination_directions_and_unread_summary(
    image_client,
    member_role,
):
    image = image_client
    set_initial_epoch(image)
    joined = BASE + timedelta(days=1)
    member = editor_membership(image)
    member.role = member_role
    member.created_at = joined
    image["session"].add(member)
    image["session"].commit()
    hidden = insert_message(image, "private before joining", joined - timedelta(microseconds=1))
    boundary = insert_message(image, "at joining", joined)
    after = insert_message(image, "after joining", joined + timedelta(microseconds=1))
    own = insert_message(image, "my message", joined + timedelta(seconds=1), image["editor"].id)

    page = read(image, "editor", limit=1).json()
    assert [message["id"] for message in page["messages"]] == [own.id]
    assert page["has_more"] is True
    assert page["unread_count"] == 2
    assert page["last_message_id"] == own.id
    assert page["last_read_message_id"] == 0
    assert datetime.fromisoformat(page["history_visible_from"]) == joined
    older = read(image, "editor", limit=1, before_id=own.id).json()
    assert [message["id"] for message in older["messages"]] == [after.id]
    oldest = read(image, "editor", before_id=after.id).json()
    assert [message["id"] for message in oldest["messages"]] == [boundary.id]
    assert oldest["has_more"] is False
    assert read(image, "editor", before_id=boundary.id).json()["messages"] == []
    forward = read(image, "editor", after_id=hidden.id, limit=1).json()
    assert [message["id"] for message in forward["messages"]] == [boundary.id]
    assert forward["has_more"] is True
    forward_rest = read(image, "editor", after_id=boundary.id).json()
    assert [message["id"] for message in forward_rest["messages"]] == [after.id, own.id]
    assert unread(image).json()["documents"] == [
        {
            "resource_id": str(image["resource"].id),
            "unread_count": 2,
            "last_message_id": own.id,
            "last_read_message_id": 0,
        }
    ]


def test_original_owner_history_starts_at_project_creation_including_exact_boundary(image_client):
    image = image_client
    set_initial_epoch(image)
    hidden = insert_message(
        image, "before project", BASE - timedelta(microseconds=1), image["editor"].id
    )
    boundary = insert_message(image, "at project creation", BASE, image["editor"].id)
    after = insert_message(image, "after project creation", BASE + timedelta(seconds=1))
    page = read(image).json()
    assert [message["id"] for message in page["messages"]] == [boundary.id, after.id]
    assert page["unread_count"] == 1
    assert datetime.fromisoformat(page["history_visible_from"]) == BASE
    assert read(image, after_id=hidden.id).json()["messages"] == page["messages"]
    assert unread(image, "owner").json()["documents"][0]["unread_count"] == 1


def test_remove_and_rejoin_resets_read_epoch_and_never_acknowledges_old_receipts(image_client):
    image = image_client
    set_initial_epoch(image)
    old = insert_message(
        image, "old confidential receipt", BASE + timedelta(days=1), image["editor"].id
    )
    insert_event(image, old)
    assert mark_read(image, old.id).status_code == 200
    removed = image["client"].delete(
        f"/api/v1/projects/{image['project'].id}/members/{image['editor'].id}",
        headers=image["headers"]["owner"],
    )
    assert removed.status_code in (200, 204)
    assert unread(image).status_code == 404
    joined = BASE + timedelta(days=2)
    image["session"].add(
        ProjectMember(
            project_id=image["project"].id,
            user_id=image["editor"].id,
            role="editor",
            created_at=joined,
        )
    )
    image["session"].commit()
    fresh = insert_message(image, "new membership", joined)
    insert_event(image, fresh)
    page = read(image, "editor").json()
    assert [message["id"] for message in page["messages"]] == [fresh.id]
    assert page["last_read_message_id"] == 0
    assert page["unread_count"] == 1
    assert unread(image).json()["documents"][0] == {
        "resource_id": str(image["resource"].id),
        "unread_count": 1,
        "last_message_id": fresh.id,
        "last_read_message_id": 0,
    }
    retry = send(image, "different fresh content", "editor", old.client_message_id)
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "chat_message_before_membership"
    assert old.body not in retry.text
    replay = pending(image).json()
    chat_events = [event for event in replay if event["event"].startswith("document.chat.")]
    assert [
        (event["event"], event["data"].get("message", {}).get("id")) for event in chat_events
    ] == [
        ("document.chat.created", fresh.id),
    ]
    assert mark_read(image, fresh.id).status_code == 200
    state = image["session"].get(DocumentChatReadState, (image["editor"].id, image["resource"].id))
    image["session"].refresh(state)
    assert aware_utc(state.joined_at) == joined
    assert state.last_read_message_id == fresh.id


def test_pending_replay_uses_real_message_date_and_scans_past_hidden_batches(image_client):
    image = image_client
    set_initial_epoch(image)
    joined = BASE + timedelta(days=1)
    member = editor_membership(image)
    member.created_at = joined
    image["session"].add(member)
    image["session"].commit()
    old = insert_message(image, "must never replay", joined - timedelta(microseconds=1))
    for _ in range(events.MAX_EVENTS_PER_BATCH + 5):
        insert_event(
            image,
            old,
            transform=lambda data: data["message"].update(
                {
                    "created_at": (joined + timedelta(days=10)).isoformat(),
                }
            ),
        )
    boundary = insert_message(image, "visible at boundary", joined)
    visible_event = insert_event(image, boundary)
    response = pending(image)
    assert response.status_code == 200
    assert [event["id"] for event in response.json()] == [visible_event.id]
    assert old.body not in response.text
    assert pending(image, after=visible_event.id).json() == []


@pytest.mark.parametrize("access_change", ["remove", "block", "outsider"])
def test_pending_replay_revalidates_current_access_even_when_event_was_persisted(
    image_client, access_change
):
    image = image_client
    set_initial_epoch(image)
    message = insert_message(image, "private durable event", BASE)
    event = insert_event(image, message)
    role = "editor"
    if access_change == "remove":
        image["session"].delete(editor_membership(image))
    elif access_change == "block":
        image["session"].add(
            ProjectBlockedUser(
                project_id=image["project"].id,
                user_id=image["editor"].id,
                blocked_by_user_id=image["project"].owner_id,
            )
        )
    else:
        outsider = (
            image["session"]
            .exec(select(User).where(User.email == "operation-outsider@example.com"))
            .one()
        )
        event.user_id = outsider.id
        image["session"].add(event)
        role = "outsider"
    image["session"].commit()
    response = pending(image, role)
    assert response.status_code == 200
    assert response.json() == []
    assert message.body not in response.text
    assert unread(image, role).status_code == (403 if access_change == "block" else 404)


@pytest.mark.parametrize(
    "malformation", ["missing_message", "wrong_resource", "boolean_id", "missing_row"]
)
def test_malformed_chat_events_fail_closed_without_blocking_other_events(
    image_client, malformation
):
    image = image_client
    set_initial_epoch(image)
    message = insert_message(image, "private content", BASE)
    row = insert_event(image, message)
    data = deepcopy(row.data)
    if malformation == "missing_message":
        data.pop("message")
    elif malformation == "wrong_resource":
        data["message"]["resource_id"] = str(uuid4())
    elif malformation == "boolean_id":
        data["message"]["id"] = True
    else:
        data["message"]["id"] = message.id + 100
    row.data = data
    # JSON equality treats True and integer 1 as equal; force the malformed
    # representation to persist so this exercises the actual replay path.
    flag_modified(row, "data")
    generic = RealtimeEventLog(
        user_id=image["editor"].id, event="workspace.updated", data={"safe": True}
    )
    image["session"].add_all([row, generic])
    image["session"].commit()
    assert [event["event"] for event in pending(image).json()] == ["workspace.updated"]


def test_sse_replay_applies_membership_filter_before_emitting_body(image_client):
    image = image_client
    set_initial_epoch(image)
    joined = BASE + timedelta(days=1)
    member = editor_membership(image)
    member.created_at = joined
    image["session"].add(member)
    image["session"].commit()
    hidden = insert_message(image, "old SSE secret", joined - timedelta(microseconds=1))
    insert_event(image, hidden)
    visible = insert_message(image, "visible SSE message", joined)
    visible_event = insert_event(image, visible)

    async def scenario():
        async def is_disconnected():
            return False

        response = await events.stream_events(
            request=SimpleNamespace(
                headers={"last-event-id": "0"}, is_disconnected=is_disconnected
            ),
            session=image["session"],
            current_user=image["editor"],
        )
        iterator = response.body_iterator
        try:
            assert await anext(iterator) == "retry: 3000\n\n"
            assert "event: connected" in await anext(iterator)
            payload = await anext(iterator)
            assert f"id: {visible_event.id}\n" in payload
            assert visible.body in payload
            assert hidden.body not in payload
        finally:
            await iterator.aclose()

    asyncio.run(scenario())


def test_old_read_event_cannot_apply_to_a_new_membership_epoch(image_client):
    image = image_client
    set_initial_epoch(image)
    data = {
        "project_id": str(image["project"].id),
        "resource_id": str(image["resource"].id),
        "history_visible_from": BASE.isoformat(),
        "unread_count": 0,
        "last_message_id": 10,
        "last_read_message_id": 10,
    }
    assert can_deliver_realtime_event(
        session=image["session"],
        user_id=image["editor"].id,
        event="document.chat.read",
        data=data,
    )
    member = editor_membership(image)
    member.created_at = BASE + timedelta(days=1)
    image["session"].add(member)
    image["session"].commit()
    assert not can_deliver_realtime_event(
        session=image["session"],
        user_id=image["editor"].id,
        event="document.chat.read",
        data=data,
    )
