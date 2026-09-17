import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.api.deps import CookieCurrentUser, SessionDep
from app.core.config import settings
from app.core.security import create_supabase_realtime_token
from app.crud import get_project_with_access_or_404
from app.models import (
    RealtimeConfigPublic,
    RealtimeEventLog,
    RealtimeEventPublic,
    RealtimePresenceConfigPublic,
    RealtimePresenceUserPublic,
)
from app.realtime import realtime_broker

router = APIRouter()
HEARTBEAT_SECONDS = 10
STREAM_MAX_SECONDS = 30
MAX_EVENTS_PER_BATCH = 50


def format_sse_event(*, event: str, data: dict, event_id: int | None = None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    encoded_data = json.dumps(data, separators=(",", ":"), default=str)
    return f"{id_line}event: {event}\ndata: {encoded_data}\n\n"


def read_last_event_id(request: Request) -> int | None:
    raw_last_event_id = request.headers.get("last-event-id")
    if not raw_last_event_id:
        return None

    try:
        return max(0, int(raw_last_event_id))
    except ValueError:
        return None


def latest_event_id(*, session: Session, user_id: UUID) -> int:
    statement = select(func.max(RealtimeEventLog.id)).where(
        RealtimeEventLog.user_id == user_id,
    )
    try:
        return int(session.exec(statement).one() or 0)
    except SQLAlchemyError:
        session.rollback()
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


@router.get("/config", response_model=RealtimeConfigPublic)
def get_realtime_config(
    response: Response,
    session: SessionDep,
    current_user: CookieCurrentUser,
) -> RealtimeConfigPublic:
    response.headers["Cache-Control"] = "no-store"
    current_event_id = latest_event_id(session=session, user_id=current_user.id)
    if not settings.supabase_realtime_enabled:
        return RealtimeConfigPublic(enabled=False, latest_event_id=current_event_id)

    expires_delta = timedelta(minutes=max(1, settings.SUPABASE_REALTIME_TOKEN_MINUTES))
    return RealtimeConfigPublic(
        enabled=True,
        supabase_url=settings.SUPABASE_URL.rstrip("/"),
        publishable_key=settings.SUPABASE_PUBLISHABLE_KEY,
        access_token=create_supabase_realtime_token(
            current_user.id,
            expires_delta=expires_delta,
        ),
        expires_at=datetime.now(UTC) + expires_delta,
        channel=f"user:{current_user.id}",
        latest_event_id=current_event_id,
    )


@router.get("/presence/config", response_model=RealtimePresenceConfigPublic)
def get_realtime_presence_config(
    response: Response,
    session: SessionDep,
    current_user: CookieCurrentUser,
    project_id: str = Query(min_length=1),
) -> RealtimePresenceConfigPublic:
    response.headers["Cache-Control"] = "no-store"
    project, _access_role = get_project_with_access_or_404(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
    )
    if not settings.supabase_realtime_enabled:
        return RealtimePresenceConfigPublic(enabled=False)

    expires_delta = timedelta(minutes=max(1, settings.SUPABASE_REALTIME_TOKEN_MINUTES))
    return RealtimePresenceConfigPublic(
        enabled=True,
        supabase_url=settings.SUPABASE_URL.rstrip("/"),
        publishable_key=settings.SUPABASE_PUBLISHABLE_KEY,
        access_token=create_supabase_realtime_token(
            current_user.id,
            expires_delta=expires_delta,
        ),
        expires_at=datetime.now(UTC) + expires_delta,
        channel=f"project:{project.id}:presence:{project.realtime_generation}",
        user=RealtimePresenceUserPublic.model_validate(current_user),
    )


@router.get("/pending", response_model=list[RealtimeEventPublic])
def get_pending_events(
    session: SessionDep,
    current_user: CookieCurrentUser,
    after_event_id: int = Query(default=0, ge=0),
) -> list[RealtimeEventLog]:
    return list_pending_events(
        session=session,
        user_id=current_user.id,
        after_event_id=after_event_id,
    )


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
    requested_event_id = read_last_event_id(request)
    last_event_id = (
        requested_event_id
        if requested_event_id is not None
        else latest_event_id(session=session, user_id=current_user.id)
    )

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
                    # The in-memory broker only wakes this stream. Reading the
                    # persisted row on the next iteration keeps SSE event ids
                    # correct and avoids delivering the same event twice.
                    continue
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
