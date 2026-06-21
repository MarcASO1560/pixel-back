import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.api.deps import CookieCurrentUser, SessionDep
from app.models import RealtimeEventLog
from app.realtime import realtime_broker

router = APIRouter()
HEARTBEAT_SECONDS = 10
STREAM_MAX_SECONDS = 30
MAX_EVENTS_PER_BATCH = 50


def format_sse_event(*, event: str, data: dict, event_id: int | None = None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    encoded_data = json.dumps(data, separators=(",", ":"), default=str)
    return f"{id_line}event: {event}\ndata: {encoded_data}\n\n"


def read_last_event_id(request: Request) -> int:
    raw_last_event_id = request.headers.get("last-event-id")
    if not raw_last_event_id:
        return 0

    try:
        return max(0, int(raw_last_event_id))
    except ValueError:
        return 0


def list_pending_events(
    *,
    session: Session,
    user_id: UUID,
    after_event_id: int,
) -> list[RealtimeEventLog]:
    statement = (
        select(RealtimeEventLog)
        .where(
            RealtimeEventLog.user_id == user_id,
            RealtimeEventLog.id > after_event_id,
        )
        .order_by(RealtimeEventLog.id)
        .limit(MAX_EVENTS_PER_BATCH)
    )
    try:
        return list(session.exec(statement).all())
    except SQLAlchemyError:
        session.rollback()
        return []


@router.get("/stream")
async def stream_events(
    request: Request,
    session: SessionDep,
    current_user: CookieCurrentUser,
) -> StreamingResponse:
    subscription = realtime_broker.subscribe(
        user_id=current_user.id,
        loop=asyncio.get_running_loop(),
    )
    last_event_id = read_last_event_id(request)

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal last_event_id

        try:
            yield "retry: 3000\n\n"
            yield format_sse_event(
                event="connected",
                data={"user_id": str(current_user.id)},
            )

            stream_deadline = asyncio.get_running_loop().time() + STREAM_MAX_SECONDS
            while asyncio.get_running_loop().time() < stream_deadline:
                if await request.is_disconnected():
                    break

                pending_events = list_pending_events(
                    session=session,
                    user_id=current_user.id,
                    after_event_id=last_event_id,
                )
                if pending_events:
                    for pending_event in pending_events:
                        if pending_event.id is None:
                            continue

                        last_event_id = pending_event.id
                        yield format_sse_event(
                            event=pending_event.event,
                            data=pending_event.data,
                            event_id=pending_event.id,
                        )
                    continue

                timeout_seconds = min(
                    HEARTBEAT_SECONDS,
                    max(0.1, stream_deadline - asyncio.get_running_loop().time()),
                )
                event = await subscription.get(timeout_seconds)
                if event:
                    yield format_sse_event(event=event.event, data=event.data)
                else:
                    yield ": heartbeat\n\n"
        finally:
            subscription.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
