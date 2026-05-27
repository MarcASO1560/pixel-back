from collections.abc import Generator
from contextlib import contextmanager

import requests
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.deps import get_db
from app.api.routes import login as login_route
from app.core import email as email_service
from app.core import security
from app.crud import create_password_reset_request
from app.main import app
from app.models import (
    PasswordCredential,
    PasswordResetRequestCreate,
    PasswordResetToken,
    User,
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
        assert response.json() == {
            "status": "ok",
            "email_sent": False,
        }
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
        assert response.json() == {
            "status": "ok",
            "email_sent": True,
        }
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
