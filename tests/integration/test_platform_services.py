import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from minio import Minio
from sqlalchemy import create_engine, text

from tennis_engine.infrastructure.database import EXPECTED_ALEMBIC_REVISION, database_ready
from tennis_engine.infrastructure.object_store import S3ObjectStore


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="isolated PostgreSQL not configured"
)
def test_postgres_upgrade_downgrade_and_append_only_history(monkeypatch):
    database_url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setenv("TENNIS_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        assert database_ready(engine) == (True, EXPECTED_ALEMBIC_REVISION)
        with engine.begin() as opened:
            opened.execute(
                text(
                    "INSERT INTO tennis.audit_event "
                    "(event_id,event_type,entity_type,entity_id,payload,actor) "
                    "VALUES (:id,'fixture','match','one','{}','pytest')"
                ),
                {"id": uuid4()},
            )
        with pytest.raises(Exception, match="append-only history"):
            with engine.begin() as opened:
                opened.execute(text("DELETE FROM tennis.audit_event"))
    finally:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("TEST_OBJECT_STORE_ENDPOINT"), reason="isolated object store not configured"
)
def test_s3_compatible_store_round_trip():
    endpoint = os.environ["TEST_OBJECT_STORE_ENDPOINT"]
    access_key = os.environ["TEST_OBJECT_STORE_ACCESS_KEY"]
    secret_key = os.environ["TEST_OBJECT_STORE_SECRET_KEY"]
    bucket = f"pytest-{uuid4()}"
    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)
    client.make_bucket(bucket)
    try:
        store = S3ObjectStore(client, bucket)
        assert store.put("fixtures/one", b"one").size_bytes == 3
        assert store.get("fixtures/one") == b"one"
        with pytest.raises(FileExistsError):
            store.put("fixtures/one", b"different")
    finally:
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
