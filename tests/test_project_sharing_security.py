"""Share-link lifetime and persistent project membership revocation."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from test_image_operations import image_client as image_client
from test_image_operations import pixel_packet

from app import crud
from app.models import (
    ProjectBlockedUser,
    ProjectMember,
    ProjectShareLink,
    RealtimeEventLog,
    ResourceEditorState,
    ResourceEditorStateUpdate,
)


def project_url(image):
    return f"/api/v1/projects/{image['project'].id}"


def share(image, values=None, role="owner"):
    return image["client"].post(
        project_url(image) + "/share-link", json=values or {}, headers=image["headers"][role]
    )


def accept(image, token, role="outsider"):
    return image["client"].post(
        f"/api/v1/projects/share-links/{token}/accept", headers=image["headers"][role]
    )


def block(image, role="owner", target_id=None):
    return image["client"].post(
        project_url(image) + f"/blocked-users/{target_id or image['editor'].id}",
        headers=image["headers"][role],
    )


def unblocked(image):
    return image["client"].delete(
        project_url(image) + f"/blocked-users/{image['editor'].id}",
        headers=image["headers"]["owner"],
    )


def test_default_expiration_and_omission_vs_explicit_null(image_client, monkeypatch):
    image = image_client
    now = datetime(2026, 9, 18, 10)
    monkeypatch.setattr(crud, "utc_now", lambda: now)
    first = share(image).json()
    assert first["expires_at"] == "2026-09-25T10:00:00Z"
    assert first["is_expired"] is False
    changed = share(image, {"role": "viewer"}).json()
    assert changed["expires_at"] == first["expires_at"]
    assert changed["token"] == first["token"]
    assert share(image).json()["role"] == "viewer"
    infinite = share(image, {"expires_at": None}).json()
    assert infinite["expires_at"] is None
    assert share(image, {"role": "editor"}).json()["expires_at"] is None


@pytest.mark.parametrize(
    "value,status",
    [
        ("2026-09-18T11:00:00", 422),
        ("2026-09-18T10:00:00Z", 400),
        ("2026-09-17T10:00:00Z", 400),
        ("2026-09-19T12:00:00+02:00", 200),
    ],
)
def test_expiration_requires_future_explicit_timezone(image_client, monkeypatch, value, status):
    monkeypatch.setattr(crud, "utc_now", lambda: datetime(2026, 9, 18, 10))
    response = share(image_client, {"expires_at": value})
    assert response.status_code == status
    if status == 200:
        assert response.json()["expires_at"] == "2026-09-19T10:00:00Z"
    elif status == 400:
        assert response.json()["detail"]["code"] == "share_link_expiration_invalid"


def test_expiration_exact_boundary_precedes_existing_membership(image_client, monkeypatch):
    image = image_client
    now = datetime(2026, 9, 18, 10)
    monkeypatch.setattr(crud, "utc_now", lambda: now)
    link = share(image, {"expires_at": "2026-09-18T11:00:00Z"}).json()
    monkeypatch.setattr(crud, "utc_now", lambda: now + timedelta(hours=1, microseconds=-1))
    assert accept(image, link["token"]).status_code == 200
    monkeypatch.setattr(crud, "utc_now", lambda: now + timedelta(hours=1))
    for role in ("outsider", "editor", "owner"):
        response = accept(image, link["token"], role)
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "share_link_expired"
    assert (
        image["client"]
        .get(project_url(image) + "/tree", headers=image["headers"]["editor"])
        .status_code
        == 200
    )
    assert (
        image["client"]
        .get(project_url(image) + "/share-link", headers=image["headers"]["owner"])
        .json()["is_expired"]
        is True
    )


@pytest.mark.parametrize("new_expiration", [None, "2026-10-01T12:00:00Z"])
def test_expired_links_cannot_resurrect_original_url(image_client, monkeypatch, new_expiration):
    image = image_client
    now = datetime(2026, 9, 18, 10)
    monkeypatch.setattr(crud, "utc_now", lambda: now)
    first = share(image, {"expires_at": "2026-09-18T11:00:00Z"}).json()
    monkeypatch.setattr(crud, "utc_now", lambda: now + timedelta(hours=2))
    denied = share(image, {"expires_at": new_expiration})
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "share_link_rotation_required"
    role_only = share(image, {"role": "viewer"}).json()
    assert role_only["token"] == first["token"] and role_only["is_expired"]
    renewed = share(image, {"rotate_token": True, "expires_at": new_expiration}).json()
    assert renewed["token"] != first["token"]
    assert renewed["role"] == "viewer" and not renewed["is_expired"]
    assert accept(image, first["token"]).status_code == 404
    assert accept(image, renewed["token"]).status_code == 200


def test_rotate_expired_without_expiry_renews_seven_days(image_client, monkeypatch):
    image = image_client
    now = datetime(2026, 9, 18, 10)
    monkeypatch.setattr(crud, "utc_now", lambda: now)
    first = share(image).json()
    monkeypatch.setattr(crud, "utc_now", lambda: now + timedelta(days=8))
    renewed = share(image, {"rotate_token": True}).json()
    assert renewed["expires_at"] == "2026-10-03T10:00:00Z"
    assert accept(image, first["token"]).status_code == 404


def test_legacy_nonexpiring_link_preserved(image_client, monkeypatch):
    image = image_client
    link = ProjectShareLink(project_id=image["project"].id, token="legacy", role="owner")
    image["session"].add(link)
    image["session"].commit()
    monkeypatch.setattr(crud, "utc_now", lambda: datetime(2050, 1, 1))
    preserved = share(image).json()
    assert preserved["expires_at"] is None and preserved["token"] == "legacy"
    assert preserved["role"] == "editor"
    assert accept(image, "legacy").status_code == 200


def test_block_persists_across_rotated_links_and_unblock_requires_rejoining(image_client):
    image = image_client
    link = share(image).json()
    old_generation = image["project"].realtime_generation
    response = block(image)
    assert response.status_code == 200
    assert response.json()["id"] == str(image["editor"].id)
    assert response.json()["blocked_at"].endswith("Z")
    assert image["project"].realtime_generation != old_generation
    assert image["session"].get(ProjectMember, (image["project"].id, image["editor"].id)) is None
    generation = image["project"].realtime_generation
    assert block(image).json() == response.json()
    assert image["project"].realtime_generation == generation
    for token in (link["token"],):
        denied = accept(image, token, "editor")
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "project_user_blocked"
    renewed_token = share(image, {"rotate_token": True}).json()["token"]
    assert accept(image, renewed_token, "editor").status_code == 403
    blocked_profiles = (
        image["client"]
        .get(project_url(image) + "/blocked-users", headers=image["headers"]["owner"])
        .json()
    )
    assert blocked_profiles == [response.json()]
    assert unblocked(image).status_code == 204
    assert unblocked(image).status_code == 204
    assert image["session"].get(ProjectMember, (image["project"].id, image["editor"].id)) is None
    assert image["client"].get(image["url"], headers=image["headers"]["editor"]).status_code == 404
    token = share(image).json()["token"]
    assert accept(image, token, "editor").status_code == 200


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", "/tree"),
        ("GET", "/access"),
        ("PATCH", ""),
        ("GET", "/resources/{resource}"),
        ("PATCH", "/resources/{resource}"),
        ("GET", "/resources/{resource}/image-operations"),
        ("POST", "/resources/{resource}/image-operations"),
        ("GET", "/resources/{resource}/editor-state"),
    ],
)
def test_block_overrides_errant_membership_every_authoritative_path(image_client, method, suffix):
    image = image_client
    assert block(image).status_code == 200
    image["session"].add(
        ProjectMember(project_id=image["project"].id, user_id=image["editor"].id, role="owner")
    )
    image["session"].commit()
    suffix = suffix.format(resource=image["resource"].id)
    payload = pixel_packet("blocked") if method == "POST" else {"name": "Denied"}
    response = image["client"].request(
        method, project_url(image) + suffix, json=payload, headers=image["headers"]["editor"]
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "project_user_blocked"
    assert image["client"].get("/api/v1/projects/", headers=image["headers"]["editor"]).json() == []
    assert image["editor"].id not in crud.list_project_user_ids(
        session=image["session"], project_id=image["project"].id
    )


def test_block_denies_presence_and_notifies_removed_target(image_client):
    image = image_client
    assert block(image).status_code == 200
    token = image["headers"]["editor"]["Authorization"].split(" ", 1)[1]
    image["client"].cookies.set("sefkira_access_token", token)
    response = image["client"].get(
        "/api/v1/events/presence/config", params={"project_id": str(image["project"].id)}
    )
    assert response.status_code == 403
    events = (
        image["session"]
        .exec(select(RealtimeEventLog).where(RealtimeEventLog.user_id == image["editor"].id))
        .all()
    )
    assert {event.event for event in events} >= {"project.access.updated", "workspace.updated"}
    assert all("realtime_generation" not in event.data for event in events)


@pytest.mark.parametrize("target", ["self", "coowner"])
def test_block_protects_primary_and_coowners(image_client, target):
    image = image_client
    if target == "self":
        user_id = image["project"].owner_id
    else:
        user_id = image["editor"].id
        member = image["session"].get(ProjectMember, (image["project"].id, user_id))
        member.role = "owner"
        image["session"].add(member)
        image["session"].commit()
    response = block(image, target_id=user_id)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "project_block_owner_protected"
    assert image["session"].exec(select(ProjectBlockedUser)).all() == []
    if target == "coowner":
        assert (
            image["client"]
            .patch(
                project_url(image) + f"/members/{user_id}",
                json={"role": "editor"},
                headers=image["headers"]["owner"],
            )
            .status_code
            == 200
        )
        assert block(image).status_code == 200


@pytest.mark.parametrize("role,status", [("editor", 403), ("viewer", 403), ("outsider", 404)])
def test_block_list_and_mutation_owner_only(image_client, role, status):
    image = image_client
    assert block(image, role=role).status_code == status
    assert (
        image["client"]
        .get(project_url(image) + "/blocked-users", headers=image["headers"][role])
        .status_code
        == status
    )
    assert (
        image["client"]
        .delete(
            project_url(image) + f"/blocked-users/{image['editor'].id}",
            headers=image["headers"][role],
        )
        .status_code
        == status
    )
    assert share(image, role=role).status_code == status


def test_block_requires_existing_member_but_coowner_can_manage(image_client):
    image = image_client
    assert block(image, target_id=uuid4()).status_code == 404
    outsider_id = crud.get_user_by_email(
        session=image["session"], email="operation-outsider@example.com"
    ).id
    assert block(image, target_id=outsider_id).status_code == 404
    viewer_id = crud.get_user_by_email(
        session=image["session"], email="operation-viewer@example.com"
    ).id
    member = image["session"].get(ProjectMember, (image["project"].id, image["editor"].id))
    member.role = "owner"
    image["session"].add(member)
    image["session"].commit()
    assert block(image, role="editor", target_id=viewer_id).status_code == 200
    assert block(image, role="editor", target_id=image["project"].owner_id).status_code == 400


@pytest.mark.parametrize("mode", ["remove", "leave"])
def test_membership_removal_rotates_room_and_normal_revoke_allows_later_join(image_client, mode):
    image = image_client
    token = share(image).json()["token"]
    generation = image["project"].realtime_generation
    suffix = f"/members/{image['editor'].id}" if mode == "remove" else "/members/me"
    role = "owner" if mode == "remove" else "editor"
    assert (
        image["client"]
        .delete(project_url(image) + suffix, headers=image["headers"][role])
        .status_code
        == 204
    )
    assert image["project"].realtime_generation != generation
    assert image["session"].exec(select(ProjectBlockedUser)).all() == []
    assert accept(image, token, "editor").status_code == 200


def test_editor_state_seed_retry_reauthorizes_after_block_during_rollback(
    image_client, monkeypatch
):
    image = image_client
    session = image["session"]
    original_commit, original_rollback = session.commit, session.rollback
    commits = [0]

    def simulate_seed_collision():
        commits[0] += 1
        if commits[0] == 1:
            raise IntegrityError("simulated concurrent state seed", {}, Exception("unique"))
        return original_commit()

    def simulate_block_committed_after_permission_fence_released():
        original_rollback()
        member = session.get(ProjectMember, (image["project"].id, image["editor"].id))
        session.delete(member)
        session.add(ProjectBlockedUser(project_id=image["project"].id, user_id=image["editor"].id))
        original_commit()

    monkeypatch.setattr(session, "commit", simulate_seed_collision)
    monkeypatch.setattr(
        session, "rollback", simulate_block_committed_after_permission_fence_released
    )
    with pytest.raises(HTTPException) as raised:
        crud.upsert_resource_editor_state(
            session=session,
            user_id=image["editor"].id,
            project_id=str(image["project"].id),
            resource_id=str(image["resource"].id),
            state_update=ResourceEditorStateUpdate(),
        )
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "project_user_blocked"
    assert session.exec(select(ResourceEditorState)).all() == []
