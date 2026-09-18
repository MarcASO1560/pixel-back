"""Opt-in chat races using generated disposable LOCAL PostgreSQL databases.

The reused fixture requires explicit IMAGE_OPERATIONS_TEST_DATABASE_URL,
rejects non-local URLs, and never connects to the application's database.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, select
from test_image_operations_postgresql import local_postgres_image as local_postgres_image
from test_project_sharing_postgresql import sharing_database as sharing_database

from app import document_chat
from app.crud import block_project_user
from app.document_chat import DocumentChatMessageCreate, create_document_chat_message
from app.models import DocumentChatMessage, Project, ProjectResource, RealtimeEventLog, User


@pytest.fixture
def chat_database(local_postgres_image, monkeypatch):
    monkeypatch.setattr(document_chat.realtime_broker, "publish", lambda **_kwargs: None)
    return local_postgres_image


def append(image, *, session, client_id=None, body="hello", author_id=None, sticker_id=None):
    author = session.get(User, author_id or image["user_id"])
    assert author is not None
    return create_document_chat_message(
        session=session,
        author=author,
        project_id=image["project_id"],
        resource_id=image["resource_id"],
        message_in=DocumentChatMessageCreate(
            client_message_id=client_id or uuid4(), body=body, sticker_id=sticker_id
        ),
    )


@pytest.mark.parametrize("sticker_id", [None, "tiny-rpg-love"])
def test_concurrent_retry_commits_one_message_and_one_recipient_event(chat_database, sticker_id):
    image = chat_database
    barrier = Barrier(2)
    client_id = uuid4()

    def send():
        with Session(image["engine"]) as session:
            session.exec(text("SET TIME ZONE 'Europe/Madrid'"))
            barrier.wait(timeout=10)
            return append(
                image, session=session, client_id=client_id,
                body="" if sticker_id else "hello", sticker_id=sticker_id,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send), executor.submit(send)]
        responses = [future.result(timeout=20) for future in futures]
    assert responses[0].model_dump() == responses[1].model_dump()
    assert responses[0].sticker_id == sticker_id
    with Session(image["engine"]) as session:
        assert len(session.exec(select(DocumentChatMessage)).all()) == 1
        assert len(session.exec(select(RealtimeEventLog)).all()) == 1
        assert session.exec(select(ProjectResource)).one().revision == 0


def test_message_ids_commit_in_order_even_when_first_commit_pauses(chat_database):
    image = chat_database
    first_at_commit, release_first, second_at_lock = Event(), Event(), Event()

    def send_first():
        with Session(image["engine"]) as session:
            original_commit = session.commit

            def paused_commit():
                first_at_commit.set()
                assert release_first.wait(timeout=10)
                original_commit()

            session.commit = paused_commit
            return append(image, session=session, body="first")

    def send_second():
        with Session(image["engine"]) as session:
            original_execute = session.execute

            def observe_lock(statement, *args, **kwargs):
                if "pg_advisory_xact_lock" in str(statement):
                    second_at_lock.set()
                return original_execute(statement, *args, **kwargs)

            session.execute = observe_lock
            return append(image, session=session, body="second")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send_first)
        try:
            assert first_at_commit.wait(timeout=10)
            second = executor.submit(send_second)
            assert second_at_lock.wait(timeout=10)
            assert not second.done()
        finally:
            release_first.set()
        first_message, second_message = first.result(timeout=20), second.result(timeout=20)
    assert first_message.id < second_message.id
    with Session(image["engine"]) as session:
        page = document_chat.list_document_chat_messages(
            session=session,
            user_id=image["user_id"],
            project_id=image["project_id"],
            resource_id=image["resource_id"],
            after_id=first_message.id,
        )
        assert [message.id for message in page.messages] == [second_message.id]


@pytest.mark.parametrize("sticker_id", [None, "tiny-rpg-love"])
def test_block_commits_before_waiting_chat_rechecks_membership(
    sharing_database, monkeypatch, sticker_id
):
    image = sharing_database
    monkeypatch.setattr(document_chat.realtime_broker, "publish", lambda **_kwargs: None)
    started = Event()

    def send():
        with Session(image["engine"]) as session:
            started.set()
            try:
                return append(
                    image, session=session, author_id=image["editor_id"],
                    body="" if sticker_id else "hello", sticker_id=sticker_id,
                )
            except HTTPException as error:
                session.rollback()
                return error

    with Session(image["engine"]) as locked_session:
        locked_session.exec(select(Project).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(send)
            assert started.wait(timeout=10)
            block_project_user(
                session=locked_session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                blocked_user_id=str(image["editor_id"]),
            )
            rejected = pending.result(timeout=20)
    assert isinstance(rejected, HTTPException) and rejected.status_code == 403
    with Session(image["engine"]) as session:
        assert session.exec(select(DocumentChatMessage)).all() == []
        assert session.exec(select(ProjectResource)).one().revision == 0
