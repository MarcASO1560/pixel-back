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
