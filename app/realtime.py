from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlmodel import Session


def can_deliver_realtime_event(
    *, session: Session, user_id: UUID, event: str, data: dict[str, Any]
) -> bool:
    """Revalidate durable chat events against the person's current membership."""
    if event not in {"document.chat.created", "document.chat.read"}:
        return True

    # Local imports avoid the chat service's dependency on the realtime broker.
    from fastapi import HTTPException
    from sqlmodel import select

    from app.document_chat import chat_history_visible_from, chat_resource_with_access
    from app.models import DocumentChatMessage

    try:
        project_id = UUID(data["project_id"])
        resource_id = UUID(data["resource_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False

    if event == "document.chat.created":
        message = data.get("message")
        if not isinstance(message, dict):
            return False
        message_id = message.get("id")
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            return False
        try:
            if (
                UUID(message["project_id"]) != project_id
                or UUID(message["resource_id"]) != resource_id
            ):
                return False
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    try:
        chat_resource_with_access(
            session=session,
            user_id=user_id,
            project_id=str(project_id),
            resource_id=str(resource_id),
        )
        visible_from = chat_history_visible_from(
            session=session,
            user_id=user_id,
            project_id=project_id,
        )
    except HTTPException:
        return False

    if event == "document.chat.read":
        epoch = data.get("history_visible_from")
        if not isinstance(epoch, str):
            return False
        try:
            event_epoch = datetime.fromisoformat(epoch)
        except ValueError:
            return False
        event_epoch = (
            event_epoch.replace(tzinfo=UTC)
            if event_epoch.tzinfo is None
            else event_epoch.astimezone(UTC)
        )
        return event_epoch == visible_from

    # The persisted message is authoritative; a replay's JSON timestamp can be
    # stale or malformed and must never widen the current membership's history.
    created_at = session.exec(
        select(DocumentChatMessage.created_at).where(
            DocumentChatMessage.id == message_id,
            DocumentChatMessage.project_id == project_id,
            DocumentChatMessage.resource_id == resource_id,
        )
    ).first()
    if created_at is None:
        return False
    created_at = (
        created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at.astimezone(UTC)
    )
    return created_at >= visible_from


@dataclass(frozen=True)
class RealtimeEvent:
    event: str
    data: dict[str, Any]


@dataclass(frozen=True)
class RealtimeSubscriptionHandle:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[RealtimeEvent]


class RealtimeSubscription:
    def __init__(
        self,
        broker: RealtimeBroker,
        user_id: UUID,
        handle: RealtimeSubscriptionHandle,
    ) -> None:
        self._broker = broker
        self.user_id = user_id
        self._handle = handle
        self._is_closed = False

    async def get(self, timeout_seconds: float) -> RealtimeEvent | None:
        try:
            return await asyncio.wait_for(self._handle.queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None

    def close(self) -> None:
        if self._is_closed:
            return

        self._is_closed = True
        self._broker.unsubscribe(self.user_id, self._handle)


class RealtimeBroker:
    def __init__(self) -> None:
        self._lock = Lock()
        self._subscriptions: dict[UUID, list[RealtimeSubscriptionHandle]] = {}

    def subscribe(
        self,
        user_id: UUID,
        loop: asyncio.AbstractEventLoop,
    ) -> RealtimeSubscription:
        handle = RealtimeSubscriptionHandle(
            loop=loop,
            queue=asyncio.Queue(maxsize=100),
        )
        with self._lock:
            self._subscriptions.setdefault(user_id, []).append(handle)
        return RealtimeSubscription(self, user_id, handle)

    def unsubscribe(self, user_id: UUID, handle: RealtimeSubscriptionHandle) -> None:
        with self._lock:
            handles = self._subscriptions.get(user_id)
            if not handles:
                return

            self._subscriptions[user_id] = [current for current in handles if current is not handle]
            if not self._subscriptions[user_id]:
                del self._subscriptions[user_id]

    def _drop_stale_handles(self, stale_handles: list[RealtimeSubscriptionHandle]) -> None:
        if not stale_handles:
            return

        stale_handle_ids = {id(handle) for handle in stale_handles}
        with self._lock:
            for user_id, handles in list(self._subscriptions.items()):
                self._subscriptions[user_id] = [
                    handle for handle in handles if id(handle) not in stale_handle_ids
                ]
                if not self._subscriptions[user_id]:
                    del self._subscriptions[user_id]

    def publish(
        self,
        *,
        user_ids: set[UUID],
        event: str,
        data: dict[str, Any],
    ) -> None:
        if not user_ids:
            return

        payload = RealtimeEvent(event=event, data=data)
        with self._lock:
            handles = [
                handle for user_id in user_ids for handle in self._subscriptions.get(user_id, [])
            ]

        stale_handles: list[RealtimeSubscriptionHandle] = []
        for handle in handles:
            if handle.loop.is_closed():
                stale_handles.append(handle)
                continue

            def deliver(
                queue: asyncio.Queue[RealtimeEvent] = handle.queue,
                event_payload: RealtimeEvent = payload,
            ) -> None:
                try:
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(event_payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

            handle.loop.call_soon_threadsafe(deliver)

        self._drop_stale_handles(stale_handles)


realtime_broker = RealtimeBroker()
