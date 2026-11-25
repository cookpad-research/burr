# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""BIP-0042: Tests for S3 buffering, settings, and SQS message parsing."""

import asyncio
import datetime
import inspect
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote_plus

import pytest

pytest.importorskip("aiobotocore")

from botocore.exceptions import ClientError
from fastapi import HTTPException
from tortoise import Tortoise

from burr import system
from burr.tracking.server.backend import EventDrivenBackendMixin
from burr.tracking.server.s3 import backend as s3_backend
from burr.tracking.server.s3.backend import (
    S3Settings,
    SQLiteS3Backend,
    TrackingMode,
    _is_indexed_key,
    _parse_sqs_message_events,
    _query_s3_file,
)
from burr.tracking.server.s3.models import Application, LogFile, Project


def _minimal_backend(**kwargs):
    defaults = dict(
        s3_bucket="test-bucket",
        update_interval_milliseconds=60_000,
        aws_max_concurrency=10,
        snapshot_interval_milliseconds=3_600_000,
        load_snapshot_on_start=False,
        prior_snapshots_to_keep=1,
        tracking_mode=TrackingMode.POLLING,
        sqs_queue_url=None,
        sqs_region=None,
    )
    defaults.update(kwargs)
    return SQLiteS3Backend(**defaults)


class TestS3Settings:
    """S3Settings BIP-0042 fields and coercion."""

    def test_s3_settings_has_tracking_mode(self):
        assert "tracking_mode" in S3Settings.model_fields
        assert S3Settings.model_fields["tracking_mode"].default == TrackingMode.POLLING

    def test_s3_settings_has_sqs_queue_url(self):
        assert "sqs_queue_url" in S3Settings.model_fields
        assert S3Settings.model_fields["sqs_queue_url"].default is None

    def test_s3_settings_has_sqs_region(self):
        assert "sqs_region" in S3Settings.model_fields
        assert S3Settings.model_fields["sqs_region"].default is None

    def test_s3_settings_has_sqs_wait_time_seconds(self):
        assert "sqs_wait_time_seconds" in S3Settings.model_fields
        assert S3Settings.model_fields["sqs_wait_time_seconds"].default == 20

    def test_s3_settings_has_s3_buffer_size_mb(self):
        assert "s3_buffer_size_mb" in S3Settings.model_fields
        assert S3Settings.model_fields["s3_buffer_size_mb"].default == 10

    def test_index_retention_days_defaults_to_keep_all(self, monkeypatch):
        assert S3Settings(s3_bucket="test").index_retention_days is None
        monkeypatch.setenv("BURR_INDEX_RETENTION_DAYS", "30")
        assert S3Settings(s3_bucket="test").index_retention_days == 30

    def test_s3_settings_coerces_sqs_string_to_event_driven(self):
        settings = S3Settings(s3_bucket="test", tracking_mode="SQS")
        assert settings.tracking_mode == TrackingMode.EVENT_DRIVEN


class TestSQLiteS3BackendInit:
    """SQLiteS3Backend constructor and mixins."""

    def test_backend_accepts_new_parameters(self):
        sig = inspect.signature(SQLiteS3Backend.__init__)
        params = list(sig.parameters.keys())
        assert "tracking_mode" in params
        assert "sqs_queue_url" in params
        assert "sqs_region" in params
        assert "sqs_wait_time_seconds" in params
        assert "s3_buffer_size_mb" in params

    def test_backend_has_event_driven_methods(self):
        assert hasattr(SQLiteS3Backend, "_handle_s3_event")
        assert hasattr(SQLiteS3Backend, "start_event_consumer")
        assert hasattr(SQLiteS3Backend, "is_event_driven")
        assert callable(getattr(SQLiteS3Backend, "_handle_s3_event"))
        assert callable(getattr(SQLiteS3Backend, "start_event_consumer"))
        assert callable(getattr(SQLiteS3Backend, "is_event_driven"))


class TestIsEventDriven:
    def test_true_when_event_driven_and_queue_url_set(self):
        b = _minimal_backend(
            tracking_mode=TrackingMode.EVENT_DRIVEN,
            sqs_queue_url="https://sqs.us-east-1.amazonaws.com/123/test",
        )
        assert b.is_event_driven() is True

    def test_false_when_polling(self):
        b = _minimal_backend(tracking_mode=TrackingMode.POLLING, sqs_queue_url=None)
        assert b.is_event_driven() is False

    def test_false_when_event_driven_but_no_queue_url(self):
        b = _minimal_backend(tracking_mode=TrackingMode.EVENT_DRIVEN, sqs_queue_url=None)
        assert b.is_event_driven() is False


class TestParseSqsMessageEvents:
    def test_eventbridge_wrapped_s3(self):
        body = {
            "detail": {"object": {"key": "data/proj/2024/01/01/00/00/pk/app/log.jsonl"}},
            "time": "2024-06-01T12:34:56Z",
        }
        events = _parse_sqs_message_events(body)
        assert events is not None
        assert len(events) == 1
        key, t = events[0]
        assert key.endswith(".jsonl")
        assert t.tzinfo is not None

    def test_native_s3_notification_multiple_records(self):
        body = {
            "Records": [
                {
                    "s3": {"object": {"key": "data/a.jsonl"}},
                    "eventTime": "2024-01-01T00:00:00.000Z",
                },
                {
                    "s3": {"object": {"key": "data/b.jsonl"}},
                    "eventTime": "2024-01-02T00:00:00.000Z",
                },
            ]
        }
        events = _parse_sqs_message_events(body)
        assert events is not None
        assert len(events) == 2
        assert events[0][0].endswith("a.jsonl")
        assert events[1][0].endswith("b.jsonl")

    def test_unknown_format_returns_none(self):
        assert _parse_sqs_message_events({"foo": "bar"}) is None

    def test_native_s3_notification_key_is_url_decoded(self):
        # S3 notifications URL-encode the key; the tracking client's time partition has ':' and '+'.
        body = {
            "Records": [
                {
                    "s3": {
                        "object": {
                            "key": "data/proj/2024/01/01/00/34%3A56.123456%2B00%3A00/pk/app/log.jsonl"
                        }
                    },
                    "eventTime": "2024-01-01T00:00:00.000Z",
                }
            ]
        }
        events = _parse_sqs_message_events(body)
        assert events is not None
        assert events[0][0] == "data/proj/2024/01/01/00/34:56.123456+00:00/pk/app/log.jsonl"


APP_PREFIX = "data/proj/2024/01/01/00/34:56.123456+00:00/pk/app"


class TestIsIndexedKey:
    @pytest.mark.parametrize(
        "key",
        [
            f"{APP_PREFIX}/0c1d__log.jsonl",
            f"{APP_PREFIX}/graph.json",
            f"{APP_PREFIX}/metadata.json",
        ],
    )
    def test_indexes_what_the_tracking_client_writes(self, key):
        assert _is_indexed_key(key, "data")

    @pytest.mark.parametrize(
        "key",
        [
            None,
            "",
            "snapshots/2024-01-01.db",
            "other/proj/app/log.jsonl",
            f"{APP_PREFIX}/notes.txt",
        ],
    )
    def test_skips_other_keys(self, key):
        assert not _is_indexed_key(key, "data")


class TestEventDrivenBackendMixin:
    def test_mixin_exists(self):
        assert EventDrivenBackendMixin is not None

    def test_mixin_has_abstract_methods(self):
        import abc

        assert issubclass(EventDrivenBackendMixin, abc.ABC)
        assert hasattr(EventDrivenBackendMixin, "start_event_consumer")
        assert hasattr(EventDrivenBackendMixin, "is_event_driven")

    def test_sqlite_s3_backend_inherits_mixin(self):
        assert issubclass(SQLiteS3Backend, EventDrivenBackendMixin)


class TestQueryS3FileBuffering:
    def test_query_s3_file_has_buffer_param(self):
        sig = inspect.signature(_query_s3_file)
        assert "buffer_size_mb" in sig.parameters
        assert sig.parameters["buffer_size_mb"].default == 10

    @pytest.mark.asyncio
    async def test_query_s3_file_reads_via_buffer(self):
        chunk = b"x" * 4096
        stream = AsyncMock()
        stream.read = AsyncMock(side_effect=[chunk, chunk, b""])

        body_cm = MagicMock()
        body_cm.__aenter__ = AsyncMock(return_value=stream)
        body_cm.__aexit__ = AsyncMock(return_value=None)

        client = AsyncMock()
        client.get_object = AsyncMock(return_value={"Body": body_cm})

        data = await _query_s3_file("bucket", "key", client, buffer_size_mb=1)
        assert data == chunk + chunk
        client.get_object.assert_called_once_with(Bucket="bucket", Key="key")


class TestHandleS3Event:
    def test_handle_s3_event_method_exists(self):
        assert hasattr(SQLiteS3Backend, "_handle_s3_event")
        method = getattr(SQLiteS3Backend, "_handle_s3_event")
        assert inspect.iscoroutinefunction(method)

    def test_handle_s3_event_signature(self):
        sig = inspect.signature(SQLiteS3Backend._handle_s3_event)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "s3_key" in params
        assert "event_time" in params


class TestStartEventConsumer:
    @pytest.mark.asyncio
    async def test_indexes_graph_metadata_and_logs_and_skips_snapshots(self):
        keys = [
            f"{APP_PREFIX}/graph.json",
            f"{APP_PREFIX}/metadata.json",
            f"{APP_PREFIX}/0c1d__log.jsonl",
            "snapshots/2024-01-01.db",
        ]
        body = {
            "Records": [
                # As S3 sends them: URL-encoded.
                {
                    "s3": {"object": {"key": quote_plus(key, safe="/")}},
                    "eventTime": "2024-01-01T00:00:00.000Z",
                }
                for key in keys
            ]
        }
        sqs = AsyncMock()
        sqs.receive_message = AsyncMock(
            side_effect=[
                {"Messages": [{"Body": json.dumps(body), "ReceiptHandle": "r1"}]},
                asyncio.CancelledError(),
            ]
        )
        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=sqs)
        client_cm.__aexit__ = AsyncMock(return_value=None)

        backend = _minimal_backend(
            tracking_mode=TrackingMode.EVENT_DRIVEN, sqs_queue_url="https://queue", sqs_region="r"
        )
        backend._session = MagicMock()
        backend._session.create_client = MagicMock(return_value=client_cm)
        backend._handle_s3_event = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await backend.start_event_consumer()

        assert [c.args[0] for c in backend._handle_s3_event.await_args_list] == keys[:3]
        sqs.delete_message.assert_awaited_once_with(QueueUrl="https://queue", ReceiptHandle="r1")


@pytest.fixture
async def index_db():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"models": ["burr.tracking.server.s3.models"]}
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


def _days_ago(days: float) -> datetime.datetime:
    return system.now() - datetime.timedelta(days=days)


async def _app(project: Project, name: str, created_days_ago: float, *log_days_ago: float):
    app = await Application.create(
        name=name,
        partition_key="pk",
        project=project,
        created_at=_days_ago(created_days_ago),
        graph_file_pointer=f"{APP_PREFIX}/graph.json",
    )
    for i, days in enumerate(log_days_ago):
        await LogFile.create(
            s3_path=f"{APP_PREFIX}/{name}-{i}__log.jsonl",
            application=app,
            tracker_id="t",
            min_sequence_id=i,
            max_sequence_id=i,
            created_at=_days_ago(days),
        )
    return app


class TestPruneIndex:
    @pytest.mark.asyncio
    async def test_removes_whole_applications_whose_first_file_expired(self, index_db):
        project = await Project.create(name="proj", created_at=_days_ago(100))
        # Its first log file is past the retention: the whole trace goes, the recent file too.
        await _app(project, "old", 31, 31, 1)
        await _app(project, "recent", 1, 1, 0)
        await _app(project, "old-without-logs", 31)
        await _app(project, "recent-without-logs", 1)

        removed = await _minimal_backend(index_retention_days=30).prune_index()

        assert removed == 2
        assert sorted(await Application.all().values_list("name", flat=True)) == [
            "recent",
            "recent-without-logs",
        ]
        assert await LogFile.all().count() == 2
        assert await Project.all().count() == 1

    @pytest.mark.asyncio
    async def test_keeps_everything_without_a_retention(self, index_db):
        project = await Project.create(name="proj", created_at=_days_ago(100))
        await _app(project, "old", 400, 400)

        assert await _minimal_backend().prune_index() == 0
        assert await Application.all().count() == 1


def _backend_with_s3(s3, **kwargs):
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=s3)
    client_cm.__aexit__ = AsyncMock(return_value=None)
    backend = _minimal_backend(**kwargs)
    backend._session = MagicMock()
    backend._session.create_client = MagicMock(return_value=client_cm)
    return backend


def _make_db(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (v TEXT)")
    db.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    db.commit()
    db.close()


def _rows(path):
    db = sqlite3.connect(path)
    try:
        return [r for (r,) in db.execute("SELECT v FROM t ORDER BY v")]
    finally:
        db.close()


class TestSnapshotPrunes:
    @pytest.mark.asyncio
    async def test_saves_the_snapshot_even_when_the_prune_fails(self, tmp_path, monkeypatch):
        db = tmp_path / "db.sqlite3"
        _make_db(db, ["a"])
        monkeypatch.setattr(s3_backend.settings, "DB_PATH", str(db))
        s3 = AsyncMock()
        backend = _backend_with_s3(s3, index_retention_days=30)
        backend.prune_index = AsyncMock(side_effect=RuntimeError("db locked"))

        await backend.snapshot()

        backend.prune_index.assert_awaited_once()
        s3.put_object.assert_awaited_once()


class TestSnapshotCopy:
    @pytest.mark.asyncio
    async def test_the_snapshot_holds_the_writes_still_in_the_wal(self, tmp_path, monkeypatch):
        path = tmp_path / "db.sqlite3"
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")  # keep every write in the WAL
        writer.execute("CREATE TABLE t (v TEXT)")
        writer.execute("INSERT INTO t VALUES ('in-the-wal')")
        writer.commit()  # the connection stays open, as the server's
        monkeypatch.setattr(s3_backend.settings, "DB_PATH", str(path))
        uploaded = {}

        async def put_object(Bucket, Key, Body):
            uploaded["body"] = Body.read()

        s3 = AsyncMock()
        s3.put_object = AsyncMock(side_effect=put_object)
        try:
            await _backend_with_s3(s3).snapshot()
        finally:
            writer.close()

        snapshot = tmp_path / "uploaded.db"
        snapshot.write_bytes(uploaded["body"])
        assert _rows(snapshot) == ["in-the-wal"]


def _snapshot_listing(key):
    return {"KeyCount": 1, "Contents": [{"Key": key}]}


def _s3_body(data: bytes):
    body = MagicMock()
    body.__aenter__ = AsyncMock(return_value=body)
    body.__aexit__ = AsyncMock(return_value=None)
    body.read = AsyncMock(return_value=data)
    return {"Body": body}


class TestLoadSnapshot:
    @pytest.mark.asyncio
    async def test_replaces_the_empty_database_made_at_start(self, tmp_path, monkeypatch):
        # The image makes an empty database (aerich init-db) before the server starts.
        path = tmp_path / "db.sqlite3"
        _make_db(path, [])
        (tmp_path / "db.sqlite3-wal").write_bytes(b"old wal")
        snapshot = tmp_path / "snapshot.db"
        _make_db(snapshot, ["from-the-snapshot"])
        monkeypatch.setattr(s3_backend.settings, "DB_PATH", str(path))
        s3 = AsyncMock()
        s3.list_objects_v2 = AsyncMock(return_value=_snapshot_listing("snapshots/1/x/snapshot.db"))
        s3.get_object = AsyncMock(return_value=_s3_body(snapshot.read_bytes()))

        await _backend_with_s3(s3, load_snapshot_on_start=True).load_snapshot()

        assert _rows(path) == ["from-the-snapshot"]
        assert not (tmp_path / "db.sqlite3-wal").exists()
        assert not (tmp_path / "db.sqlite3.download").exists()
        s3.get_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="snapshots/1/x/snapshot.db"
        )

    @pytest.mark.asyncio
    async def test_keeps_the_database_without_a_snapshot(self, tmp_path, monkeypatch):
        path = tmp_path / "db.sqlite3"
        _make_db(path, ["local"])
        monkeypatch.setattr(s3_backend.settings, "DB_PATH", str(path))
        s3 = AsyncMock()
        s3.list_objects_v2 = AsyncMock(return_value={"KeyCount": 0})

        await _backend_with_s3(s3, load_snapshot_on_start=True).load_snapshot()

        assert _rows(path) == ["local"]
        s3.get_object.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_nothing_when_turned_off(self, tmp_path, monkeypatch):
        path = tmp_path / "db.sqlite3"
        _make_db(path, ["local"])
        monkeypatch.setattr(s3_backend.settings, "DB_PATH", str(path))
        s3 = AsyncMock()

        await _backend_with_s3(s3, load_snapshot_on_start=False).load_snapshot()

        assert _rows(path) == ["local"]
        s3.list_objects_v2.assert_not_awaited()


def _s3_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


class TestGetApplicationLogsMissingFiles:
    def _backend_with_s3_error(self, code: str):
        s3 = AsyncMock()
        s3.get_object = AsyncMock(side_effect=_s3_error(code))
        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=s3)
        client_cm.__aexit__ = AsyncMock(return_value=None)
        backend = _minimal_backend()
        backend._session = MagicMock()
        backend._session.create_client = MagicMock(return_value=client_cm)
        return backend

    @pytest.mark.asyncio
    async def test_a_file_gone_from_s3_is_a_404(self, index_db):
        project = await Project.create(name="proj", created_at=_days_ago(1))
        await _app(project, "app", 1, 1)
        backend = self._backend_with_s3_error("NoSuchKey")

        with pytest.raises(HTTPException) as error:
            await backend.get_application_logs(MagicMock(), "proj", "app", "pk")
        assert error.value.status_code == 404

    @pytest.mark.asyncio
    async def test_other_s3_errors_are_not_hidden(self, index_db):
        project = await Project.create(name="proj", created_at=_days_ago(1))
        await _app(project, "app", 1, 1)
        backend = self._backend_with_s3_error("AccessDenied")

        with pytest.raises(ClientError):
            await backend.get_application_logs(MagicMock(), "proj", "app", "pk")

    @pytest.mark.asyncio
    async def test_an_application_not_in_the_index_is_a_404(self, index_db):
        await Project.create(name="proj", created_at=_days_ago(1))

        with pytest.raises(HTTPException) as error:
            await _minimal_backend().get_application_logs(MagicMock(), "proj", "gone", "pk")
        assert error.value.status_code == 404


class TestScanWithoutFiles:
    @pytest.mark.asyncio
    async def test_a_project_with_no_files_left_does_not_stop_the_scan(self, index_db):
        # A snapshot can hold a project whose files have all expired; it has no index status.
        await Project.create(name="expired", created_at=_days_ago(400), indexed_at=_days_ago(400))
        backend = _minimal_backend()
        backend._gather_paths_to_update = AsyncMock(return_value=[])

        await backend._scan_and_update_db()

        backend._gather_paths_to_update.assert_awaited_once()
