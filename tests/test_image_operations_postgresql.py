"""Opt-in concurrency proof against an isolated, disposable LOCAL PostgreSQL DB.

Set IMAGE_OPERATIONS_TEST_DATABASE_URL to a localhost PostgreSQL role with
CREATE DATABASE permission. The configured application/production DB is NEVER
used. Tests create and drop only a fresh image_ops_test_<uuid> database.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine, select

from app import image_operations
from app.crud import update_project_resource
from app.image_operations import ImageOperationRequest, submit_image_operation
from app.models import ImageOperationReceipt, Project, ProjectResource, ProjectResourceUpdate, User


@pytest.fixture
def local_postgres_image(monkeypatch):
    configured_url = os.environ.get("IMAGE_OPERATIONS_TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("Explicit localhost-only PostgreSQL test URL is not configured")
    url = make_url(configured_url)
    if not url.drivername.startswith("postgresql") or url.host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("Refusing non-local IMAGE_OPERATIONS_TEST_DATABASE_URL")
    url = url.set(drivername="postgresql+psycopg")
    database_name = "image_ops_test_" + uuid4().hex
    assert re.fullmatch(r"image_ops_test_[0-9a-f]{32}", database_name)
    admin = create_engine(url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
        engine = create_engine(url.set(database=database_name), connect_args={"connect_timeout": 5})
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr(image_operations, "publish_project_event", lambda **_kwargs: None)
        with Session(engine) as session:
            owner = User(email="local-concurrency-test@example.com")
            session.add(owner)
            session.flush()
            project = Project(owner_id=owner.id, name="Local concurrency test")
            session.add(project)
            session.flush()
            resource = ProjectResource(
                project_id=project.id,
                name="Image",
                type="pixel_art",
                data={
                    "pixel_art": {
                        "version": 2,
                        "width": 2,
                        "height": 1,
                        "palette": [],
                        "layers": [
                            {
                                "id": "base",
                                "name": "Layer 1",
                                "visible": True,
                                "locked": False,
                                "opacity": 1,
                                "pixels": [None, None],
                            }
                        ],
                    }
                },
            )
            session.add(resource)
            session.commit()
            yield {
                "engine": engine,
                "user_id": owner.id,
                "project_id": str(project.id),
                "resource_id": str(resource.id),
            }
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # Exact target was generated and validated above; never drop a
            # configured/existing database, and never force other connections.
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database_name}"'))
        admin.dispose()


def operation(identifier: str, index: int = 0, color: str = "#FF0000"):
    return ImageOperationRequest.model_validate(
        {
            "operation_id": identifier,
            "base_revision": 0,
            "width": 2,
            "height": 1,
            "actions": [{"type": "pixels", "layer_id": "base", "changes": [[index, color]]}],
        }
    )


def test_simultaneous_disjoint_packets_preserve_both_pixels(local_postgres_image):
    image = local_postgres_image
    barrier = Barrier(2)

    def send(packet):
        with Session(image["engine"]) as session:
            barrier.wait(timeout=10)
            return submit_image_operation(
                session=session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=packet,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send, operation("first", 0))
        second = executor.submit(send, operation("second", 1, "#00FF00"))
        responses = [first.result(timeout=20), second.result(timeout=20)]
    assert {response.applied_revision for response in responses} == {1, 2}
    with Session(image["engine"]) as session:
        resource = session.exec(select(ProjectResource)).one()
        assert resource.revision == 2
        assert resource.data["pixel_art"]["layers"][0]["pixels"] == ["#FF0000", "#00FF00"]


def test_simultaneous_duplicate_is_applied_once(local_postgres_image):
    image = local_postgres_image
    barrier = Barrier(2)

    def send():
        with Session(image["engine"]) as session:
            barrier.wait(timeout=10)
            return submit_image_operation(
                session=session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=operation("duplicate"),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send), executor.submit(send)]
        responses = [future.result(timeout=20) for future in futures]
    assert [response.applied_revision for response in responses] == [1, 1]
    with Session(image["engine"]) as session:
        assert session.exec(select(ProjectResource)).one().revision == 1
        assert len(session.exec(select(ImageOperationReceipt)).all()) == 1


def test_locked_legacy_patch_cannot_overwrite_first_accepted_operation(local_postgres_image):
    image = local_postgres_image
    attempted = Event()

    def stale_patch():
        with Session(image["engine"]) as session:
            attempted.set()
            try:
                update_project_resource(
                    session=session,
                    user_id=image["user_id"],
                    project_id=image["project_id"],
                    resource_id=image["resource_id"],
                    resource_update=ProjectResourceUpdate(data={"pixel_art": {"version": 1}}),
                )
            except HTTPException as error:
                session.rollback()
                return error
        raise AssertionError("Legacy overwrite unexpectedly succeeded")

    with Session(image["engine"]) as locked_session:
        locked_session.exec(select(ProjectResource).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(stale_patch)
            assert attempted.wait(timeout=10)
            # The old writer is queued on the same resource row lock; the
            # first operation and receipt commit before its journal check.
            response = submit_image_operation(
                session=locked_session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=operation("first-operation"),
            )
            assert response.applied_revision == 1
            rejected = future.result(timeout=20)
    assert rejected.status_code == 409
    assert rejected.detail["code"] == "image_operations_required"
    with Session(image["engine"]) as session:
        resource = session.exec(select(ProjectResource)).one()
        assert resource.revision == 1
        assert resource.data["pixel_art"]["layers"][0]["pixels"] == ["#FF0000", None]


def test_concurrent_semantic_resize_and_stale_pixels_preserve_both(local_postgres_image):
    image = local_postgres_image
    barrier = Barrier(2)
    resize = ImageOperationRequest.model_validate(
        {
            "operation_id": "resize",
            "base_revision": 0,
            "width": 2,
            "height": 1,
            "actions": [{"type": "resize", "width": 3, "height": 2, "anchor": "center"}],
        }
    )

    def send(packet):
        with Session(image["engine"]) as session:
            barrier.wait(timeout=10)
            return submit_image_operation(
                session=session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=packet,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(send, resize),
            executor.submit(send, operation("paint", 1, "#00FF00")),
        ]
        assert {future.result(timeout=20).applied_revision for future in futures} == {1, 2}
    with Session(image["engine"]) as session:
        resource = session.exec(select(ProjectResource)).one()
        document = resource.data["pixel_art"]
        assert (document["width"], document["height"]) == (3, 2)
        assert document["layers"][0]["pixels"][5] == "#00FF00"


def test_concurrent_global_undo_serializes_shared_history(local_postgres_image):
    image = local_postgres_image
    with Session(image["engine"]) as session:
        for packet in [operation("paint-a", 0), operation("paint-b", 1, "#00FF00")]:
            submit_image_operation(
                session=session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=packet,
            )
    barrier = Barrier(2)

    def undo(identifier):
        with Session(image["engine"]) as session:
            barrier.wait(timeout=10)
            packet = ImageOperationRequest.model_validate(
                {
                    "operation_id": identifier,
                    "base_revision": 0,
                    "width": 2,
                    "height": 1,
                    "actions": [{"type": "undo"}],
                }
            )
            return submit_image_operation(
                session=session,
                user_id=image["user_id"],
                project_id=image["project_id"],
                resource_id=image["resource_id"],
                packet=packet,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(undo, "undo-a"), executor.submit(undo, "undo-b")]
        assert {future.result(timeout=20).applied_revision for future in futures} == {3, 4}
    with Session(image["engine"]) as session:
        resource = session.exec(select(ProjectResource)).one()
        assert resource.data["pixel_art"]["layers"][0]["pixels"] == [None, None]
