"""Opt-in sharing-security races in generated disposable LOCAL PostgreSQL DBs.

The reused fixture rejects every non-local URL and never uses application DB
configuration. IMAGE_OPERATIONS_TEST_DATABASE_URL must be explicitly supplied.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, select
from test_image_operations_postgresql import local_postgres_image as local_postgres_image
from test_image_operations_postgresql import operation

from app import crud
from app.crud import (
    accept_project_share_link,
    block_project_user,
    get_or_create_project_share_link,
    get_project_or_404,
)
from app.image_operations import submit_image_operation
from app.models import (
    Project,
    ProjectBlockedUser,
    ProjectMember,
    ProjectResource,
    ProjectShareLinkCreate,
    User,
)


@pytest.fixture
def sharing_database(local_postgres_image, monkeypatch):
    image = local_postgres_image
    monkeypatch.setattr(crud, "publish_project_event", lambda **_kwargs: None)
    with Session(image["engine"]) as session:
        editor = User(email="local-sharing-member@example.com")
        outsider = User(email="local-sharing-outsider@example.com")
        session.add_all([editor, outsider])
        session.flush()
        session.add(ProjectMember(project_id=UUID(image["project_id"]), user_id=editor.id))
        session.commit()
        image["editor_id"] = editor.id
        image["outsider_id"] = outsider.id
        image["token"] = get_or_create_project_share_link(
            session=session, user_id=image["user_id"], project_id=image["project_id"]
        ).token
    return image


def attempt_accept(image, user_id=None, started=None):
    with Session(image["engine"]) as session:
        if started:
            started.set()
        try:
            return accept_project_share_link(
                session=session, user_id=user_id or image["editor_id"], token=image["token"]
            )
        except HTTPException as error:
            session.rollback()
            return error


def attempt_block(image, started=None):
    with Session(image["engine"]) as session:
        if started:
            started.set()
        return block_project_user(
            session=session,
            user_id=image["user_id"],
            project_id=image["project_id"],
            blocked_user_id=str(image["editor_id"]),
        )


def attempt_paint(image, started=None):
    with Session(image["engine"]) as session:
        if started:
            started.set()
        try:
            return submit_image_operation(
                session=session,
                user_id=image["editor_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=operation("permission-fenced-paint"),
            )
        except HTTPException as error:
            session.rollback()
            return error


def assert_blocked_without_membership(image):
    with Session(image["engine"]) as session:
        key = (UUID(image["project_id"]), image["editor_id"])
        assert session.get(ProjectBlockedUser, key) is not None
        assert session.get(ProjectMember, key) is None


def test_concurrent_join_and_block_never_recreate_membership(sharing_database):
    image = sharing_database
    barrier = Barrier(2)

    def join():
        barrier.wait(timeout=10)
        return attempt_accept(image)

    def ban():
        barrier.wait(timeout=10)
        return attempt_block(image)

    with ThreadPoolExecutor(max_workers=2) as executor:
        join_future, block_future = executor.submit(join), executor.submit(ban)
        joined = join_future.result(timeout=20)
        block_future.result(timeout=20)
    if isinstance(joined, HTTPException):
        assert joined.status_code == 403 and joined.detail["code"] == "project_user_blocked"
    assert_blocked_without_membership(image)


def test_rotation_waiting_accept_reloads_token_after_project_lock(sharing_database):
    image = sharing_database
    started = Event()
    with Session(image["engine"]) as locked_session:
        locked_session.exec(select(Project).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(attempt_accept, image, image["outsider_id"], started)
            assert started.wait(timeout=10)
            renewed = get_or_create_project_share_link(
                session=locked_session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                share_link_create=ProjectShareLinkCreate(rotate_token=True),
            )
            assert renewed.token != image["token"]
            rejected = pending.result(timeout=20)
    assert isinstance(rejected, HTTPException) and rejected.status_code == 404
    with Session(image["engine"]) as session:
        assert session.get(ProjectMember, (UUID(image["project_id"]), image["outsider_id"])) is None


def test_accept_expiration_checked_after_waiting_for_project_lock(sharing_database, monkeypatch):
    image = sharing_database
    clock = [datetime(2026, 9, 18, 10)]
    monkeypatch.setattr(crud, "utc_now", lambda: clock[0])
    with Session(image["engine"]) as session:
        image["token"] = get_or_create_project_share_link(
            session=session,
            user_id=image["user_id"],
            project_id=image["project_id"],
            share_link_create=ProjectShareLinkCreate(expires_at="2026-09-18T11:00:00Z"),
        ).token
    started = Event()
    with Session(image["engine"]) as locked_session:
        locked_session.exec(select(Project).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(attempt_accept, image, image["outsider_id"], started)
            assert started.wait(timeout=10)
            clock[0] += timedelta(hours=1)
            locked_session.commit()
            rejected = pending.result(timeout=20)
    assert isinstance(rejected, HTTPException) and rejected.status_code == 410


def test_block_first_fences_waiting_image_packet(sharing_database):
    image = sharing_database
    started = Event()
    with Session(image["engine"]) as locked_session:
        locked_session.exec(select(Project).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(attempt_paint, image, started)
            assert started.wait(timeout=10)
            block_project_user(
                session=locked_session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                blocked_user_id=str(image["editor_id"]),
            )
            rejected = pending.result(timeout=20)
    assert isinstance(rejected, HTTPException) and rejected.status_code == 403
    assert_blocked_without_membership(image)
    with Session(image["engine"]) as session:
        resource = session.exec(select(ProjectResource)).one()
        assert resource.revision == 0
        assert resource.data["pixel_art"]["layers"][0]["pixels"] == [None, None]


def test_paint_first_commits_before_waiting_block(sharing_database):
    image = sharing_database
    started = Event()
    with Session(image["engine"]) as locked_session:
        get_project_or_404(
            session=locked_session, user_id=image["editor_id"], project_id=image["project_id"]
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(attempt_block, image, started)
            assert started.wait(timeout=10)
            accepted = submit_image_operation(
                session=locked_session,
                user_id=image["editor_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=operation("paint-before-block"),
            )
            assert accepted.applied_revision == 1
            pending.result(timeout=20)
    assert_blocked_without_membership(image)
    with Session(image["engine"]) as session:
        assert (
            session.exec(select(ProjectResource)).one().data["pixel_art"]["layers"][0]["pixels"][0]
            == "#FF0000"
        )


def test_realtime_function_rejects_stale_generation_suffixes_and_blocked_members(sharing_database):
    image = sharing_database
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/20260918_0021_project_sharing_security.py"
    )
    spec = spec_from_file_location("sharing_security_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Session(image["engine"]) as session:
        session.execute(text(migration.PROJECT_ACCESS_FUNCTION))
        project = session.exec(select(Project)).one()
        topic = f"project:{project.id}:presence:{project.realtime_generation}"

        def access(user_id, channel=topic):
            session.execute(
                text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": f'{{"sub":"{user_id}"}}'},
            )
            return session.execute(
                text("SELECT public.can_access_realtime_project(:topic)"), {"topic": channel}
            ).scalar_one()

        assert access(image["user_id"]) is True
        assert access(image["editor_id"]) is True
        assert access(image["outsider_id"]) is False
        assert access(image["editor_id"], f"project:{project.id}:presence") is False
        assert access(image["editor_id"], topic + ":extra") is False
        session.add(
            ProjectBlockedUser(
                project_id=project.id, user_id=image["editor_id"], blocked_by=image["user_id"]
            )
        )
        session.flush()
        assert access(image["editor_id"]) is False
        assert access(image["user_id"]) is True
        # A permission change abandons the old room even for remaining owners.
        project.realtime_generation = uuid4()
        session.add(project)
        session.flush()
        assert access(image["user_id"]) is False
        assert (
            access(image["user_id"], f"project:{project.id}:presence:{project.realtime_generation}")
            is True
        )
