"""The sticker schema change preserves messages from the previous backend."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_sticker_migration_preserves_existing_text_and_allows_legacy_inserts():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20260918_0023_document_chat_stickers.py"
    )
    spec = spec_from_file_location("document_chat_sticker_migration", path)
    assert spec is not None and spec.loader is not None
    revision = module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == "20260918_0022"
    engine = sa.create_engine("sqlite://")
    legacy = sa.Table(
        "document_chat_messages", sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("body", sa.String(2000), nullable=False),
    )
    legacy.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(legacy.insert(), {"id": 1, "body": "existing conversation"})
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
            sticker_column = next(
                column for column in sa.inspect(connection).get_columns("document_chat_messages")
                if column["name"] == "sticker_id"
            )
            assert sticker_column["nullable"] is True
            assert sticker_column["type"].length == 80
            # A previous deployment can keep appending text after this migration.
            connection.execute(legacy.insert(), {"id": 2, "body": "legacy client"})
            rows = connection.execute(
                sa.text("SELECT id, body, sticker_id FROM document_chat_messages ORDER BY id")
            ).all()
            assert rows == [(1, "existing conversation", None), (2, "legacy client", None)]
    finally:
        engine.dispose()


def test_read_state_migration_is_additive_and_does_not_change_existing_conversations():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/20260918_0025_document_chat_read_states.py"
    )
    spec = spec_from_file_location("document_chat_read_migration", path)
    assert spec is not None and spec.loader is not None
    revision = module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == "20260918_0024"
    engine = sa.create_engine("sqlite://")
    legacy = sa.MetaData()
    projects = sa.Table("projects", legacy, sa.Column("id", sa.Uuid(), primary_key=True))
    messages = sa.Table(
        "document_chat_messages", legacy,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("body", sa.String(2000), nullable=False),
    )
    legacy.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(messages.insert(), {"id": 1, "body": "existing conversation"})
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
            inspector = sa.inspect(connection)
            assert "document_chat_read_states" in inspector.get_table_names()
            assert inspector.get_pk_constraint("document_chat_read_states")[
                "constrained_columns"
            ] == ["user_id", "resource_id"]
            foreign_keys = inspector.get_foreign_keys("document_chat_read_states")
            assert len(foreign_keys) == 3
            assert all(key["options"]["ondelete"] == "CASCADE" for key in foreign_keys)
            owner_date = next(column for column in inspector.get_columns(projects.name)
                              if column["name"] == "owner_joined_at")
            assert owner_date["nullable"] is True
            connection.execute(messages.insert(), {"id": 2, "body": "legacy backend still works"})
            assert connection.execute(sa.select(messages.c.id, messages.c.body)).all() == [
                (1, "existing conversation"), (2, "legacy backend still works"),
            ]
            with Operations.context(MigrationContext.configure(connection)):
                revision.downgrade()
            assert "document_chat_read_states" not in sa.inspect(connection).get_table_names()
            assert connection.execute(sa.select(messages.c.id, messages.c.body)).all()[0] == (
                1, "existing conversation",
            )
    finally:
        engine.dispose()
