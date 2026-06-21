from collections.abc import Generator
from contextlib import contextmanager
from uuid import UUID

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.deps import get_db
from app.api.routes import login as login_route
from app.core import email as email_service
from app.core import security
from app.core.config import Settings
from app.crud import create_password_reset_request, create_user
from app.main import app
from app.models import (
    PasswordCredential,
    PasswordResetRequestCreate,
    PasswordResetToken,
    Project,
    ProjectFolder,
    ProjectMember,
    ProjectResource,
    ProjectShareLink,
    RealtimeEventLog,
    ResourceExport,
    ResourceRevision,
    User,
    UserCreate,
)


@contextmanager
def create_auth_client() -> Generator[tuple[TestClient, Session], None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            PasswordCredential.__table__,
            PasswordResetToken.__table__,
            Project.__table__,
            ProjectFolder.__table__,
            ProjectMember.__table__,
            ProjectResource.__table__,
            ProjectShareLink.__table__,
            ResourceExport.__table__,
            ResourceRevision.__table__,
            RealtimeEventLog.__table__,
        ],
    )

    with Session(engine) as test_session:
        def override_get_db() -> Generator[Session, None, None]:
            yield test_session

        app.dependency_overrides[get_db] = override_get_db

        try:
            yield TestClient(app), test_session
        finally:
            app.dependency_overrides.clear()


def test_google_session_rejects_invalid_credential() -> None:
    client = TestClient(app)

    response = client.post("/api/v1/auth/google", json={"credential": "invalid"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid Google credential"}


def test_google_access_token_accepts_google_tokeninfo_audience(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    def fake_get(url: str, **_kwargs) -> FakeResponse:
        if "tokeninfo" in url:
            return FakeResponse(
                {
                    "audience": "google-client-id",
                    "email": "google@example.com",
                    "verified_email": True,
                }
            )
        raise requests.RequestException

    monkeypatch.setattr(security.settings, "GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setattr(security.requests, "get", fake_get)

    user = security.verify_google_access_token("access-token")

    assert user is not None
    assert user.email == "google@example.com"


def test_google_session_accepts_verified_access_token(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    def fake_get(url: str, **_kwargs) -> FakeResponse:
        if "tokeninfo" in url:
            return FakeResponse(
                {
                    "audience": "google-client-id",
                    "email": "google-session@example.com",
                    "verified_email": True,
                }
            )
        return FakeResponse(
            {
                "email": "google-session@example.com",
                "email_verified": True,
                "picture": "https://example.com/avatar.png",
            }
        )

    monkeypatch.setattr(security.settings, "GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setattr(security.requests, "get", fake_get)

    with create_auth_client() as (client, _session):
        response = client.post("/api/v1/auth/google", json={"access_token": "access-token"})

        assert response.status_code == 200
        access_token = response.json()["access_token"]

        me_response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert me_response.status_code == 200
        assert me_response.json()["email"] == "google-session@example.com"
        assert me_response.json()["is_admin"] is False


def test_legacy_frontend_session_endpoint_is_not_available() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/session",
        json={
            "auth_token": "public-token",
            "email": "spoofed@example.com",
            "is_admin": True,
        },
    )

    assert response.status_code == 404


def test_register_with_password_creates_session() -> None:
    with create_auth_client() as (client, _session):
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "Sefkira",
                "email": "user@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )

        assert response.status_code == 200
        access_token = response.json()["access_token"]

        me_response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert me_response.status_code == 200
        assert me_response.json()["username"] == "sefkira"
        assert me_response.json()["email"] == "user@example.com"


def test_current_user_can_update_profile_and_pixel_avatar() -> None:
    with create_auth_client() as (client, _session):
        register_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "avatar_user",
                "email": "avatar@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        access_token = register_response.json()["access_token"]

        response = client.patch(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "username": "pixel_artist",
                "avatar_pixel_art": {
                    "version": 1,
                    "size": 16,
                    "pixels": ["#000000", None],
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["username"] == "pixel_artist"
        assert response.json()["avatar_pixel_art"]["size"] == 16


def test_email_password_session_requires_valid_password() -> None:
    with create_auth_client() as (client, _session):
        client.post(
            "/api/v1/auth/register",
            json={
                "username": "artist",
                "email": "artist@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )

        bad_response = client.post(
            "/api/v1/auth/login",
            json={"email": "artist@example.com", "password": "wrong-pass"},
        )
        good_response = client.post(
            "/api/v1/auth/login",
            json={"email": "artist@example.com", "password": "secret-pass"},
        )

        assert bad_response.status_code == 401
        assert bad_response.json() == {"detail": "Invalid email or password"}
        assert good_response.status_code == 200
        assert good_response.json()["token_type"] == "bearer"


def test_authenticated_user_cannot_create_admin_user() -> None:
    with create_auth_client() as (client, session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "normal_user",
                "email": "normal@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        access_token = owner_response.json()["access_token"]

        response = client.post(
            "/api/v1/users/",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "email": "admin-escalation@example.com",
                "is_admin": True,
            },
        )
        escalated_user = session.exec(
            select(User).where(User.email == "admin-escalation@example.com"),
        ).first()

        assert response.status_code in {404, 405}
        assert escalated_user is None


def test_user_create_does_not_persist_admin_flag() -> None:
    with create_auth_client() as (_client, session):
        user = create_user(
            session=session,
            user_create=UserCreate.model_validate(
                {
                    "email": "internal-admin-flag@example.com",
                    "is_admin": True,
                },
            ),
        )

        assert user.is_admin is False


def test_non_local_secret_key_must_be_configured() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(ENVIRONMENT="production", SECRET_KEY="change-this-secret-key")

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(ENVIRONMENT="production", SECRET_KEY="too-short")

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(VERCEL_ENV="production", SECRET_KEY="change-this-secret-key")

    settings = Settings(ENVIRONMENT="production", SECRET_KEY="x" * 32)

    assert settings.SECRET_KEY == "x" * 32


def test_password_reset_request_stores_token_without_leaking_it(monkeypatch) -> None:
    monkeypatch.setattr(login_route, "send_password_reset_email", lambda *_args: False)

    with create_auth_client() as (client, session):
        client.post(
            "/api/v1/auth/register",
            json={
                "username": "reset_user",
                "email": "reset@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )

        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "reset@example.com"},
        )
        reset_tokens = session.exec(select(PasswordResetToken)).all()

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert len(reset_tokens) == 1


def test_password_reset_request_sends_email_without_leaking_token(monkeypatch) -> None:
    sent_email = {}

    def fake_send_password_reset_email(email: str, token: str) -> bool:
        sent_email["email"] = email
        sent_email["token"] = token
        return True

    monkeypatch.setattr(
        login_route,
        "send_password_reset_email",
        fake_send_password_reset_email,
    )

    with create_auth_client() as (client, session):
        client.post(
            "/api/v1/auth/register",
            json={
                "username": "email_reset",
                "email": "email-reset@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )

        response = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "email-reset@example.com"},
        )
        reset_tokens = session.exec(select(PasswordResetToken)).all()

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert sent_email["email"] == "email-reset@example.com"
        assert sent_email["token"]
        assert len(reset_tokens) == 1


def test_resend_password_reset_email_uses_configured_sender(monkeypatch) -> None:
    sent_request = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, **kwargs) -> FakeResponse:
        sent_request["url"] = url
        sent_request["headers"] = kwargs["headers"]
        sent_request["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr(email_service.settings, "RESEND_API_KEY", "resend-api-key")
    monkeypatch.setattr(
        email_service.settings,
        "RESEND_FROM_EMAIL",
        "Sefkira Studio <no-reply@sefkirastudio.com>",
    )
    monkeypatch.setattr(email_service.requests, "post", fake_post)

    sent = email_service.send_password_reset_email_with_resend(
        "user@example.com",
        "https://sefkirastudio.com?reset_token=reset-token",
    )

    assert sent is True
    assert sent_request["url"] == "https://api.resend.com/emails"
    assert sent_request["headers"]["Authorization"] == "Bearer resend-api-key"
    assert sent_request["json"]["from"] == "Sefkira Studio <no-reply@sefkirastudio.com>"
    assert sent_request["json"]["to"] == ["user@example.com"]
    assert "reset-token" in sent_request["json"]["text"]


def test_password_reset_can_update_password_from_email_token(monkeypatch) -> None:
    monkeypatch.setattr(login_route, "send_password_reset_email", lambda *_args: False)

    with create_auth_client() as (client, session):
        client.post(
            "/api/v1/auth/register",
            json={
                "username": "change_password",
                "email": "change@example.com",
                "password": "old-secret",
                "password_confirmation": "old-secret",
            },
        )

        reset_request = client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": "change@example.com"},
        )
        reset_token = create_password_reset_request(
            session=session,
            reset_request=PasswordResetRequestCreate(email="change@example.com"),
        )

        reset_response = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={
                "token": reset_token,
                "password": "new-secret",
                "password_confirmation": "new-secret",
            },
        )
        old_login_response = client.post(
            "/api/v1/auth/login",
            json={"email": "change@example.com", "password": "old-secret"},
        )
        new_login_response = client.post(
            "/api/v1/auth/login",
            json={"email": "change@example.com", "password": "new-secret"},
        )

        assert reset_request.status_code == 200
        assert "reset_token" not in reset_request.json()
        assert reset_token
        assert reset_response.status_code == 200
        assert old_login_response.status_code == 401
        assert new_login_response.status_code == 200


def test_project_owner_can_share_project_with_link() -> None:
    with create_auth_client() as (client, _session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "owner_user",
                "email": "owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        recipient_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "shared_user",
                "email": "shared@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        owner_token = owner_response.json()["access_token"]
        recipient_token = recipient_response.json()["access_token"]
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        recipient_headers = {"Authorization": f"Bearer {recipient_token}"}

        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Shared project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]

        share_response = client.post(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
        )
        share_token = share_response.json()["token"]
        accept_response = client.post(
            f"/api/v1/projects/share-links/{share_token}/accept",
            headers=recipient_headers,
        )
        recipient_projects_response = client.get("/api/v1/projects/", headers=recipient_headers)
        owner_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=owner_headers,
        )
        recipient_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=recipient_headers,
        )
        edit_response = client.patch(
            f"/api/v1/projects/{project_id}",
            headers=recipient_headers,
            json={"name": "Edited by shared user"},
        )
        delete_response = client.delete(
            f"/api/v1/projects/{project_id}",
            headers=recipient_headers,
        )
        disable_share_response = client.delete(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
        )
        disabled_accept_response = client.post(
            f"/api/v1/projects/share-links/{share_token}/accept",
            headers=recipient_headers,
        )

        assert share_response.status_code == 200
        assert "/share/" in share_response.json()["url"]
        assert share_response.json()["role"] == "editor"
        assert share_token
        assert accept_response.status_code == 200
        assert accept_response.json()["access_role"] == "editor"
        assert recipient_projects_response.status_code == 200
        assert recipient_projects_response.json()[0]["access_role"] == "editor"
        assert owner_access_response.status_code == 200
        assert recipient_access_response.status_code == 200
        assert [user["role"] for user in owner_access_response.json()] == ["owner", "editor"]
        assert [user["username"] for user in recipient_access_response.json()] == [
            "owner_user",
            "shared_user",
        ]
        assert edit_response.status_code == 200
        assert edit_response.json()["name"] == "Edited by shared user"
        assert delete_response.status_code == 403
        assert disable_share_response.status_code == 204
        assert disabled_accept_response.status_code == 404


def test_project_owner_can_persist_folder_and_resource_items() -> None:
    with create_auth_client() as (client, _session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "item_owner",
                "email": "items@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        owner_headers = {"Authorization": f"Bearer {owner_response.json()['access_token']}"}

        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Item project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]

        folder_response = client.post(
            f"/api/v1/projects/{project_id}/folders",
            headers=owner_headers,
            json={
                "name": "Sprites",
                "color": "#ffd76f",
                "position": 1,
                "parent_id": None,
            },
        )
        folder_id = folder_response.json()["id"]
        resource_response = client.post(
            f"/api/v1/projects/{project_id}/resources",
            headers=owner_headers,
            json={
                "name": "Hero",
                "type": "pixel_art",
                "resource_metadata": {"kind": "image"},
                "thumbnail_url": None,
                "color": "#ff8a72",
                "position": 1,
                "folder_id": folder_id,
                "data": {"pixels": []},
            },
        )
        resource_id = resource_response.json()["id"]
        edited_resource_response = client.patch(
            f"/api/v1/projects/{project_id}/resources/{resource_id}",
            headers=owner_headers,
            json={"name": "Hero idle", "color": "#79b8ff"},
        )
        resource_detail_response = client.get(
            f"/api/v1/projects/{project_id}/resources/{resource_id}",
            headers=owner_headers,
        )
        tree_response = client.get(
            f"/api/v1/projects/{project_id}/tree",
            headers=owner_headers,
        )

        assert folder_response.status_code == 200
        assert folder_response.json()["name"] == "Sprites"
        assert resource_response.status_code == 200
        assert resource_response.json()["folder_id"] == folder_id
        assert resource_response.json()["color"] == "#ff8a72"
        assert edited_resource_response.status_code == 200
        assert edited_resource_response.json()["name"] == "Hero idle"
        assert edited_resource_response.json()["color"] == "#79b8ff"
        assert resource_detail_response.status_code == 200
        assert resource_detail_response.json()["data"] == {"pixels": []}
        assert tree_response.status_code == 200
        assert [folder["id"] for folder in tree_response.json()["folders"]] == [folder_id]
        assert [resource["id"] for resource in tree_response.json()["resources"]] == [resource_id]


def test_project_owner_can_move_items_between_folders() -> None:
    with create_auth_client() as (client, _session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "move_owner",
                "email": "move-items@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        owner_headers = {"Authorization": f"Bearer {owner_response.json()['access_token']}"}

        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Move project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]

        parent_folder_response = client.post(
            f"/api/v1/projects/{project_id}/folders",
            headers=owner_headers,
            json={"name": "Sprites", "color": "#ffd76f", "position": 1, "parent_id": None},
        )
        child_folder_response = client.post(
            f"/api/v1/projects/{project_id}/folders",
            headers=owner_headers,
            json={"name": "Characters", "color": "#79b8ff", "position": 2, "parent_id": None},
        )
        resource_response = client.post(
            f"/api/v1/projects/{project_id}/resources",
            headers=owner_headers,
            json={
                "name": "Hero",
                "type": "pixel_art",
                "resource_metadata": {},
                "thumbnail_url": None,
                "color": "#ff8a72",
                "position": 1,
                "folder_id": None,
                "data": {},
            },
        )
        parent_folder_id = parent_folder_response.json()["id"]
        child_folder_id = child_folder_response.json()["id"]
        resource_id = resource_response.json()["id"]

        moved_resource_response = client.patch(
            f"/api/v1/projects/{project_id}/resources/{resource_id}",
            headers=owner_headers,
            json={"folder_id": parent_folder_id},
        )
        moved_folder_response = client.patch(
            f"/api/v1/projects/{project_id}/folders/{child_folder_id}",
            headers=owner_headers,
            json={"parent_id": parent_folder_id},
        )
        cycle_response = client.patch(
            f"/api/v1/projects/{project_id}/folders/{parent_folder_id}",
            headers=owner_headers,
            json={"parent_id": child_folder_id},
        )
        root_resource_response = client.patch(
            f"/api/v1/projects/{project_id}/resources/{resource_id}",
            headers=owner_headers,
            json={"folder_id": None},
        )
        root_folder_response = client.patch(
            f"/api/v1/projects/{project_id}/folders/{child_folder_id}",
            headers=owner_headers,
            json={"parent_id": None},
        )
        tree_response = client.get(
            f"/api/v1/projects/{project_id}/tree",
            headers=owner_headers,
        )

        assert moved_resource_response.status_code == 200
        assert moved_resource_response.json()["folder_id"] == parent_folder_id
        assert moved_folder_response.status_code == 200
        assert moved_folder_response.json()["parent_id"] == parent_folder_id
        assert cycle_response.status_code == 400
        assert root_resource_response.status_code == 200
        assert root_resource_response.json()["folder_id"] is None
        assert root_folder_response.status_code == 200
        assert root_folder_response.json()["parent_id"] is None
        folders_by_id = {folder["id"]: folder for folder in tree_response.json()["folders"]}
        resources_by_id = {
            resource["id"]: resource for resource in tree_response.json()["resources"]
        }
        assert folders_by_id[child_folder_id]["parent_id"] is None
        assert resources_by_id[resource_id]["folder_id"] is None


def test_project_owner_can_delete_resources_and_folders() -> None:
    with create_auth_client() as (client, _session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "delete_owner",
                "email": "delete-items@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        owner_headers = {"Authorization": f"Bearer {owner_response.json()['access_token']}"}

        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Delete project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]

        parent_folder_response = client.post(
            f"/api/v1/projects/{project_id}/folders",
            headers=owner_headers,
            json={"name": "Sprites", "color": "#ffd76f", "position": 1, "parent_id": None},
        )
        child_folder_response = client.post(
            f"/api/v1/projects/{project_id}/folders",
            headers=owner_headers,
            json={
                "name": "Characters",
                "color": "#79b8ff",
                "position": 2,
                "parent_id": parent_folder_response.json()["id"],
            },
        )
        loose_resource_response = client.post(
            f"/api/v1/projects/{project_id}/resources",
            headers=owner_headers,
            json={
                "name": "Loose",
                "type": "pixel_art",
                "resource_metadata": {},
                "thumbnail_url": None,
                "color": "#ff8a72",
                "position": 1,
                "folder_id": None,
                "data": {},
            },
        )
        nested_resource_response = client.post(
            f"/api/v1/projects/{project_id}/resources",
            headers=owner_headers,
            json={
                "name": "Nested",
                "type": "pixel_art",
                "resource_metadata": {},
                "thumbnail_url": None,
                "color": "#ff8a72",
                "position": 2,
                "folder_id": child_folder_response.json()["id"],
                "data": {},
            },
        )

        deleted_resource_response = client.delete(
            f"/api/v1/projects/{project_id}/resources/{loose_resource_response.json()['id']}",
            headers=owner_headers,
        )
        deleted_folder_response = client.delete(
            f"/api/v1/projects/{project_id}/folders/{parent_folder_response.json()['id']}",
            headers=owner_headers,
        )
        tree_response = client.get(
            f"/api/v1/projects/{project_id}/tree",
            headers=owner_headers,
        )
        missing_nested_resource_response = client.get(
            f"/api/v1/projects/{project_id}/resources/{nested_resource_response.json()['id']}",
            headers=owner_headers,
        )

        assert deleted_resource_response.status_code == 204
        assert deleted_folder_response.status_code == 204
        assert tree_response.status_code == 200
        assert tree_response.json()["folders"] == []
        assert tree_response.json()["resources"] == []
        assert missing_nested_resource_response.status_code == 404


def test_legacy_owner_share_link_only_grants_editor_access() -> None:
    with create_auth_client() as (client, session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "legacy_owner",
                "email": "legacy-owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        recipient_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "legacy_recipient",
                "email": "legacy-recipient@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        owner_headers = {"Authorization": f"Bearer {owner_response.json()['access_token']}"}
        recipient_headers = {
            "Authorization": f"Bearer {recipient_response.json()['access_token']}",
        }
        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Legacy share project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]
        session.add(
            ProjectShareLink(
                project_id=UUID(project_id),
                token="legacy-owner-share-token",
                role="owner",
            ),
        )
        session.commit()

        share_response = client.get(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
        )
        accept_response = client.post(
            "/api/v1/projects/share-links/legacy-owner-share-token/accept",
            headers=recipient_headers,
        )

        assert share_response.status_code == 200
        assert share_response.json()["role"] == "editor"
        assert accept_response.status_code == 200
        assert accept_response.json()["access_role"] == "editor"


def test_project_share_roles_can_be_managed_and_members_can_leave() -> None:
    with create_auth_client() as (client, _session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "role_owner",
                "email": "role-owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        viewer_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "viewer_user",
                "email": "viewer@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        coowner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "coowner_user",
                "email": "coowner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )

        owner_headers = {
            "Authorization": f"Bearer {owner_response.json()['access_token']}",
        }
        viewer_headers = {
            "Authorization": f"Bearer {viewer_response.json()['access_token']}",
        }
        coowner_headers = {
            "Authorization": f"Bearer {coowner_response.json()['access_token']}",
        }

        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Role managed project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]

        viewer_link_response = client.post(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
            json={"role": "viewer"},
        )
        viewer_accept_response = client.post(
            f"/api/v1/projects/share-links/{viewer_link_response.json()['token']}/accept",
            headers=viewer_headers,
        )
        viewer_edit_response = client.patch(
            f"/api/v1/projects/{project_id}",
            headers=viewer_headers,
            json={"name": "Viewer edit should fail"},
        )

        owner_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=owner_headers,
        )
        viewer_access = next(
            user for user in owner_access_response.json() if user["username"] == "viewer_user"
        )
        promote_response = client.patch(
            f"/api/v1/projects/{project_id}/members/{viewer_access['id']}",
            headers=owner_headers,
            json={"role": "editor"},
        )
        editor_edit_response = client.patch(
            f"/api/v1/projects/{project_id}",
            headers=viewer_headers,
            json={"name": "Edited after promotion"},
        )
        leave_response = client.delete(
            f"/api/v1/projects/{project_id}/members/me",
            headers=viewer_headers,
        )
        left_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=viewer_headers,
        )

        owner_link_response = client.post(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
            json={"role": "owner"},
        )
        coowner_link_response = client.post(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
            json={"role": "editor"},
        )
        coowner_accept_response = client.post(
            f"/api/v1/projects/share-links/{coowner_link_response.json()['token']}/accept",
            headers=coowner_headers,
        )
        coowner_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=coowner_headers,
        )
        coowner_access = next(
            user for user in coowner_access_response.json() if user["username"] == "coowner_user"
        )
        promote_coowner_response = client.patch(
            f"/api/v1/projects/{project_id}/members/{coowner_access['id']}",
            headers=owner_headers,
            json={"role": "owner"},
        )
        remove_coowner_response = client.delete(
            f"/api/v1/projects/{project_id}/members/{coowner_access['id']}",
            headers=owner_headers,
        )
        removed_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=coowner_headers,
        )

        assert viewer_link_response.status_code == 200
        assert viewer_link_response.json()["role"] == "viewer"
        assert viewer_accept_response.status_code == 200
        assert viewer_accept_response.json()["access_role"] == "viewer"
        assert viewer_edit_response.status_code == 403
        assert promote_response.status_code == 200
        assert promote_response.json()["role"] == "editor"
        assert editor_edit_response.status_code == 200
        assert editor_edit_response.json()["name"] == "Edited after promotion"
        assert leave_response.status_code == 204
        assert left_access_response.status_code == 404
        assert owner_link_response.status_code == 400
        assert owner_link_response.json() == {
            "detail": "Share links can only grant viewer or editor access",
        }
        assert coowner_link_response.status_code == 200
        assert coowner_link_response.json()["role"] == "editor"
        assert coowner_accept_response.status_code == 200
        assert coowner_accept_response.json()["access_role"] == "editor"
        assert coowner_access_response.status_code == 200
        assert promote_coowner_response.status_code == 200
        assert promote_coowner_response.json()["role"] == "owner"
        assert remove_coowner_response.status_code == 204
        assert removed_access_response.status_code == 404


def test_project_owner_leaves_shared_project_and_transfers_ownership() -> None:
    with create_auth_client() as (client, _session):
        owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "departing_owner",
                "email": "departing-owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        next_owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "next_owner",
                "email": "next-owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        owner_headers = {
            "Authorization": f"Bearer {owner_response.json()['access_token']}",
        }
        next_owner_headers = {
            "Authorization": f"Bearer {next_owner_response.json()['access_token']}",
        }

        project_response = client.post(
            "/api/v1/projects/",
            headers=owner_headers,
            json={
                "name": "Transferable project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]
        share_response = client.post(
            f"/api/v1/projects/{project_id}/share-link",
            headers=owner_headers,
            json={"role": "editor"},
        )
        accept_response = client.post(
            f"/api/v1/projects/share-links/{share_response.json()['token']}/accept",
            headers=next_owner_headers,
        )
        delete_shared_response = client.delete(
            f"/api/v1/projects/{project_id}",
            headers=owner_headers,
        )
        leave_response = client.delete(
            f"/api/v1/projects/{project_id}/members/me",
            headers=owner_headers,
        )
        departed_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=owner_headers,
        )
        next_owner_projects_response = client.get(
            "/api/v1/projects/",
            headers=next_owner_headers,
        )
        next_owner_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=next_owner_headers,
        )

        assert project_response.status_code == 200
        assert project_response.json()["access_count"] == 1
        assert accept_response.status_code == 200
        assert accept_response.json()["access_count"] == 2
        assert delete_shared_response.status_code == 400
        assert leave_response.status_code == 204
        assert departed_access_response.status_code == 404
        assert next_owner_projects_response.status_code == 200
        assert next_owner_projects_response.json()[0]["access_role"] == "owner"
        assert next_owner_projects_response.json()[0]["access_count"] == 1
        assert next_owner_access_response.status_code == 200
        assert [user["username"] for user in next_owner_access_response.json()] == ["next_owner"]
        assert [user["role"] for user in next_owner_access_response.json()] == ["owner"]


def test_project_owner_can_demote_original_owner_when_another_owner_exists() -> None:
    with create_auth_client() as (client, _session):
        original_owner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "original_owner",
                "email": "original-owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        coowner_response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "managing_owner",
                "email": "managing-owner@example.com",
                "password": "secret-pass",
                "password_confirmation": "secret-pass",
            },
        )
        original_owner_headers = {
            "Authorization": f"Bearer {original_owner_response.json()['access_token']}",
        }
        coowner_headers = {
            "Authorization": f"Bearer {coowner_response.json()['access_token']}",
        }

        project_response = client.post(
            "/api/v1/projects/",
            headers=original_owner_headers,
            json={
                "name": "Owner demotion project",
                "description": None,
                "settings": {},
                "thumbnail_url": None,
            },
        )
        project_id = project_response.json()["id"]
        owner_link_response = client.post(
            f"/api/v1/projects/{project_id}/share-link",
            headers=original_owner_headers,
            json={"role": "editor"},
        )
        client.post(
            f"/api/v1/projects/share-links/{owner_link_response.json()['token']}/accept",
            headers=coowner_headers,
        )
        initial_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=original_owner_headers,
        )
        coowner = next(
            user for user in initial_access_response.json() if user["username"] == "managing_owner"
        )
        promote_coowner_response = client.patch(
            f"/api/v1/projects/{project_id}/members/{coowner['id']}",
            headers=original_owner_headers,
            json={"role": "owner"},
        )
        access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=coowner_headers,
        )
        original_owner = next(
            user for user in access_response.json() if user["username"] == "original_owner"
        )

        demote_response = client.patch(
            f"/api/v1/projects/{project_id}/members/{original_owner['id']}",
            headers=coowner_headers,
            json={"role": "editor"},
        )
        original_owner_projects_response = client.get(
            "/api/v1/projects/",
            headers=original_owner_headers,
        )
        coowner_projects_response = client.get(
            "/api/v1/projects/",
            headers=coowner_headers,
        )
        updated_access_response = client.get(
            f"/api/v1/projects/{project_id}/access",
            headers=coowner_headers,
        )

        assert promote_coowner_response.status_code == 200
        assert promote_coowner_response.json()["role"] == "owner"
        assert demote_response.status_code == 200
        assert demote_response.json()["role"] == "editor"
        assert demote_response.json()["is_owner"] is False
        assert original_owner_projects_response.status_code == 200
        assert original_owner_projects_response.json()[0]["access_role"] == "editor"
        assert coowner_projects_response.status_code == 200
        assert coowner_projects_response.json()[0]["access_role"] == "owner"
        assert [user["username"] for user in updated_access_response.json()] == [
            "managing_owner",
            "original_owner",
        ]
        assert [user["role"] for user in updated_access_response.json()] == ["owner", "editor"]
