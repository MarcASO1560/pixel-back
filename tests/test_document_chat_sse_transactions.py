"""A waiting event stream cannot retain document membership or replay locks."""

import asyncio
from types import SimpleNamespace

import pytest
from test_document_chat import send
from test_image_operations import image_client as image_client

from app.api.routes import events


def test_sse_releases_initial_and_replay_transactions_before_yield_and_wait(
    image_client, monkeypatch,
):
    image = image_client
    assert send(image, "Message for replay").status_code == 200
    session = image["session"]
    editor = image["editor"]
    editor_id = editor.id  # Materialize identity before stream closes authentication's read.
    closed = []

    class FinishedWaiting(Exception):
        pass

    class Subscription:
        async def get(self, timeout_seconds):
            assert timeout_seconds > 0
            assert not session.in_transaction()
            raise FinishedWaiting

        def close(self):
            closed.append(True)

    monkeypatch.setattr(events.realtime_broker, "subscribe", lambda **_kwargs: Subscription())

    async def scenario():
        async def is_disconnected():
            return False

        response = await events.stream_events(
            request=SimpleNamespace(
                headers={"last-event-id": "0"}, is_disconnected=is_disconnected,
            ),
            session=session, current_user=editor,
        )
        assert not session.in_transaction()
        iterator = response.body_iterator
        assert await anext(iterator) == "retry: 3000\n\n"
        assert str(editor_id) in await anext(iterator)
        payload = await anext(iterator)
        assert "event: document.chat.created" in payload
        assert "Message for replay" in payload
        assert not session.in_transaction()
        with pytest.raises(FinishedWaiting):
            await anext(iterator)
        assert not session.in_transaction()
        assert closed == [True]

    asyncio.run(scenario())
