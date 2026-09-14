import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from fastapi import Request, Response

from app.api.routes import events
from app.api.routes.events import read_last_event_id
from app.core import security
from app.realtime import RealtimeBroker


def test_event_stream_only_replays_when_a_last_event_id_is_provided() -> None:
    fresh_request = Request({"type": "http", "headers": []})
    resumed_request = Request(
        {"type": "http", "headers": [(b"last-event-id", b"42")]},
    )
    invalid_request = Request(
        {"type": "http", "headers": [(b"last-event-id", b"invalid")]},
    )

    assert read_last_event_id(fresh_request) is None
    assert read_last_event_id(resumed_request) == 42
    assert read_last_event_id(invalid_request) is None


def test_realtime_broker_delivers_user_scoped_events() -> None:
    async def scenario() -> None:
        broker = RealtimeBroker()
        user_id = uuid4()
        other_user_id = uuid4()
        subscription = broker.subscribe(user_id=user_id, loop=asyncio.get_running_loop())
        other_subscription = broker.subscribe(
            user_id=other_user_id,
            loop=asyncio.get_running_loop(),
        )

        broker.publish(
            user_ids={user_id},
            event="project.updated",
            data={"project_id": "project-1"},
        )

        event = await subscription.get(timeout_seconds=1)
        other_event = await other_subscription.get(timeout_seconds=0.01)

        assert event is not None
        assert event.event == "project.updated"
        assert event.data == {"project_id": "project-1"}
        assert other_event is None

        subscription.close()
        broker.publish(
            user_ids={user_id},
            event="project.updated",
            data={"project_id": "project-2"},
        )

        assert await subscription.get(timeout_seconds=0.01) is None

    asyncio.run(scenario())


def test_supabase_realtime_token_contains_private_channel_claims(monkeypatch) -> None:
    user_id = uuid4()
    signing_secret = "test-supabase-signing-secret-long-enough"
    monkeypatch.setattr(security.settings, "SUPABASE_JWT_SECRET", signing_secret)

    token = security.create_supabase_realtime_token(
        user_id,
        expires_delta=timedelta(minutes=15),
    )
    payload = jwt.decode(
        token,
        signing_secret,
        algorithms=[security.ALGORITHM],
        audience="authenticated",
    )

    assert payload["sub"] == str(user_id)
    assert payload["role"] == "authenticated"
    assert payload["iat"] < payload["exp"]


def test_realtime_config_enables_supabase_and_starts_at_latest_event(
    monkeypatch,
) -> None:
    user_id = uuid4()
    monkeypatch.setattr(events.settings, "SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setattr(events.settings, "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setattr(
        events.settings,
        "SUPABASE_JWT_SECRET",
        "realtime-signing-secret-long-enough",
    )
    monkeypatch.setattr(events, "latest_event_id", lambda **_kwargs: 27)

    response = Response()
    config = events.get_realtime_config(
        response=response,
        session=object(),  # type: ignore[arg-type]
        current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
    )

    assert config.enabled is True
    assert config.supabase_url == "https://project.supabase.co"
    assert config.publishable_key == "sb_publishable_test"
    assert config.channel == f"user:{user_id}"
    assert config.latest_event_id == 27
    assert config.access_token
    assert config.expires_at and config.expires_at.tzinfo is not None
    assert response.headers["cache-control"] == "no-store"


def test_realtime_config_keeps_sse_fallback_when_supabase_is_incomplete(
    monkeypatch,
) -> None:
    monkeypatch.setattr(events.settings, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(events.settings, "SUPABASE_PUBLISHABLE_KEY", None)
    monkeypatch.setattr(events.settings, "SUPABASE_JWT_SECRET", None)
    monkeypatch.setattr(events, "latest_event_id", lambda **_kwargs: 4)

    config = events.get_realtime_config(
        response=Response(),
        session=object(),  # type: ignore[arg-type]
        current_user=SimpleNamespace(id=uuid4()),  # type: ignore[arg-type]
    )

    assert config.enabled is False
    assert config.latest_event_id == 4
    assert config.access_token is None


def test_realtime_presence_config_is_scoped_to_an_accessible_project(monkeypatch) -> None:
    user_id = uuid4()
    project_id = uuid4()
    current_user = SimpleNamespace(
        id=user_id,
        username="pixel_artist",
        email="artist@example.com",
        avatar_url="https://example.com/avatar.png",
        avatar_pixel_art={"version": 1, "size": 1, "palette": [], "pixels": ["#fff"]},
    )
    monkeypatch.setattr(events.settings, "SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setattr(events.settings, "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setattr(
        events.settings,
        "SUPABASE_JWT_SECRET",
        "realtime-signing-secret-long-enough",
    )
    access_checks: list[dict] = []

    def check_access(**kwargs):
        access_checks.append(kwargs)
        return SimpleNamespace(id=project_id), "editor"

    monkeypatch.setattr(
        events,
        "get_project_with_access_or_404",
        check_access,
    )

    response = Response()
    config = events.get_realtime_presence_config(
        response=response,
        session=object(),  # type: ignore[arg-type]
        current_user=current_user,  # type: ignore[arg-type]
        project_id=str(project_id),
    )

    assert access_checks[0]["user_id"] == user_id
    assert access_checks[0]["project_id"] == str(project_id)
    assert config.enabled is True
    assert config.channel == f"project:{project_id}:presence"
    assert config.user and config.user.id == user_id
    assert config.user.username == "pixel_artist"
    assert config.user.avatar_pixel_art == current_user.avatar_pixel_art
    assert config.access_token
    assert response.headers["cache-control"] == "no-store"


def test_realtime_presence_config_still_checks_access_when_disabled(monkeypatch) -> None:
    user_id = uuid4()
    project_id = uuid4()
    monkeypatch.setattr(events.settings, "SUPABASE_URL", None)
    monkeypatch.setattr(events.settings, "SUPABASE_PUBLISHABLE_KEY", None)
    monkeypatch.setattr(events.settings, "SUPABASE_JWT_SECRET", None)
    access_checks: list[dict] = []

    def check_access(**kwargs):
        access_checks.append(kwargs)
        return SimpleNamespace(id=project_id), "viewer"

    monkeypatch.setattr(events, "get_project_with_access_or_404", check_access)

    config = events.get_realtime_presence_config(
        response=Response(),
        session=object(),  # type: ignore[arg-type]
        current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
        project_id=str(project_id),
    )

    assert access_checks[0]["user_id"] == user_id
    assert config.enabled is False
    assert config.channel is None
    assert config.user is None


def test_supabase_realtime_token_requires_a_signing_secret(monkeypatch) -> None:
    monkeypatch.setattr(security.settings, "SUPABASE_JWT_SECRET", None)

    with pytest.raises(ValueError, match="SUPABASE_JWT_SECRET"):
        security.create_supabase_realtime_token(
            uuid4(),
            expires_delta=timedelta(minutes=15),
        )
