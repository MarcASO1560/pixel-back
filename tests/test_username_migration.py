"""Longer free names preserve the old schema and enforce collisions atomically."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_image_operations_postgresql import local_postgres_image as local_postgres_image


def test_username_migration_preserves_names_and_legacy_index_and_rejects_case_collisions():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20260918_0024_free_profile_names.py"
    )
    spec = spec_from_file_location("free_profile_names_migration", path)
    assert spec is not None and spec.loader is not None
    revision = module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == "20260918_0023"
    engine = sa.create_engine("sqlite://")
    legacy = sa.Table(
        "users", sa.MetaData(), sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(40), nullable=True),
    )
    sa.Index("ix_users_username", legacy.c.username, unique=True)
    legacy.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(legacy.insert(), {"id": 1, "username": "legacy_name"})
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
            username_column = next(
                column for column in sa.inspect(connection).get_columns("users")
                if column["name"] == "username"
            )
            assert username_column["nullable"] is True
            assert username_column["type"].length == 255
            assert connection.execute(
                sa.text("SELECT username FROM users")
            ).scalar() == "legacy_name"
            indexes = dict(connection.execute(sa.text(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='users'"
            )).all())
            assert "UNIQUE" in indexes["ix_users_username"]
            assert "lower(username)" in indexes["ix_users_username_case_insensitive"]
            # The previous application can keep writing short normalized names.
            connection.execute(legacy.insert(), {"id": 2, "username": "old_backend"})
            exact = "  " + "😀" * 251 + "  "
            connection.execute(legacy.insert(), {"id": 3, "username": exact})
            connection.execute(legacy.insert(), [{"id": 4}, {"id": 5}])
            assert connection.execute(sa.text(
                "SELECT username FROM users WHERE id=3"
            )).scalar() == exact
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(legacy.insert(), {"id": 6, "username": "LEGACY_NAME"})
    finally:
        engine.dispose()


def test_postgresql_username_migration_preserves_rows_and_enforces_real_length_and_uniqueness(
    local_postgres_image,
):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20260918_0024_free_profile_names.py"
    )
    spec = spec_from_file_location("postgresql_free_profile_names_migration", path)
    assert spec is not None and spec.loader is not None
    revision = module_from_spec(spec)
    spec.loader.exec_module(revision)
    # This fixture creates and drops only a generated localhost-only test database.
    with local_postgres_image["engine"].begin() as connection:
        # The shared fixture keeps a reader on public.users alive until teardown.
        # A separate schema exercises the exact migration without waiting on that reader.
        connection.execute(sa.text("SET LOCAL lock_timeout='5s'"))
        connection.execute(sa.text("CREATE SCHEMA profile_names_migration_test"))
        connection.execute(sa.text("SET LOCAL search_path TO profile_names_migration_test"))
        legacy = sa.Table(
            "users", sa.MetaData(), sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String(40), nullable=True),
            sa.Column("email", sa.String(255), nullable=False),
        )
        sa.Index("ix_users_username", legacy.c.username, unique=True)
        legacy.create(connection)
        connection.execute(legacy.insert(), {
            "id": 1, "username": "legacy_name", "email": "legacy@example.com",
        })
        before = connection.execute(sa.text("SELECT id, email, username FROM users")).all()
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        assert connection.execute(sa.text("SELECT id, email, username FROM users")).all() == before
        column = next(
            item for item in sa.inspect(connection).get_columns("users")
            if item["name"] == "username"
        )
        assert column["nullable"] is True and column["type"].length == 255
        indexes = {
            item["name"]: item for item in sa.inspect(connection).get_indexes("users")
        }
        assert indexes["ix_users_username"]["unique"] is True
        assert indexes["ix_users_username_case_insensitive"]["unique"] is True
        expressions = indexes["ix_users_username_case_insensitive"]["expressions"]
        assert len(expressions) == 1
        assert "lower(" in expressions[0] and "username" in expressions[0]
        exact = "  " + "😀" * 251 + "  "
        connection.execute(legacy.insert(), [
            {"id": 2, "username": exact, "email": "long-name@example.com"},
            {"id": 3, "username": None, "email": "absent-one@example.com"},
            {"id": 4, "username": None, "email": "absent-two@example.com"},
        ])
        assert connection.execute(sa.text(
            "SELECT username FROM users WHERE email='long-name@example.com'"
        )).scalar() == exact
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(legacy.insert(), {
                "id": 5, "username": "LEGACY_NAME", "email": "collision@example.com",
            })
        with pytest.raises(sa.exc.DataError), connection.begin_nested():
            connection.execute(legacy.insert(), {
                "id": 6, "username": "😀" * 256, "email": "too-long@example.com",
            })
        # An explicit rollback of the migration cannot silently truncate free names.
        with pytest.raises(sa.exc.DataError), connection.begin_nested():
            with Operations.context(MigrationContext.configure(connection)):
                revision.downgrade()
        assert connection.execute(sa.text(
            "SELECT username FROM users WHERE email='long-name@example.com'"
        )).scalar() == exact
