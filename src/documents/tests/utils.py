import dataclasses
import shutil
import tempfile
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Union
from unittest import mock

import httpx
import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.test import override_settings

from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.parsers import ParseError


@dataclasses.dataclass
class PaperlessDirectories:
    data_dir: Path
    scratch_dir: Path
    media_dir: Path
    consumption_dir: Path
    static_dir: Path
    index_dir: Path = dataclasses.field(init=False)
    originals_dir: Path = dataclasses.field(init=False)
    thumbnail_dir: Path = dataclasses.field(init=False)
    archive_dir: Path = dataclasses.field(init=False)
    logging_dir: Path = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.index_dir = self.data_dir / "index"
        self.originals_dir = self.media_dir / "documents" / "originals"
        self.thumbnail_dir = self.media_dir / "documents" / "thumbnails"
        self.archive_dir = self.media_dir / "documents" / "archive"
        self.logging_dir = self.data_dir / "log"

        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.logging_dir.mkdir(parents=True, exist_ok=True)

        self.settings_override = override_settings(
            DATA_DIR=self.data_dir,
            SCRATCH_DIR=self.scratch_dir,
            MEDIA_ROOT=self.media_dir,
            ORIGINALS_DIR=self.originals_dir,
            THUMBNAIL_DIR=self.thumbnail_dir,
            ARCHIVE_DIR=self.archive_dir,
            CONSUMPTION_DIR=self.consumption_dir,
            LOGGING_DIR=self.logging_dir,
            INDEX_DIR=self.index_dir,
            STATIC_ROOT=self.static_dir,
            MODEL_FILE=self.data_dir / "classification_model.pickle",
            MEDIA_LOCK=self.media_dir / "media.lock",
        )
        self.settings_override.enable()

    def cleanup(self):
        shutil.rmtree(self.media_dir, ignore_errors=True)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.scratch_dir, ignore_errors=True)
        shutil.rmtree(self.consumption_dir, ignore_errors=True)
        shutil.rmtree(self.static_dir, ignore_errors=True)
        self.settings_override.disable()


@contextmanager
def paperless_environment():
    dirs = None
    try:
        dirs = PaperlessDirectories(
            data_dir=Path(tempfile.mkdtemp()),
            scratch_dir=Path(tempfile.mkdtemp()),
            media_dir=Path(tempfile.mkdtemp()),
            consumption_dir=Path(tempfile.mkdtemp()),
            static_dir=Path(tempfile.mkdtemp()),
        )
        yield dirs
    finally:
        if dirs:
            dirs.cleanup()


def util_call_with_backoff(
    method_or_callable: Callable,
    args: Union[list, tuple],
    *,
    skip_on_50x_err=True,
) -> tuple[bool, Any]:
    """
    For whatever reason, the images started during the test pipeline like to
    segfault sometimes, crash and otherwise fail randomly, when run with the
    exact files that usually pass.

    So, this function will retry the given method/function up to 3 times, with larger backoff
    periods between each attempt, in hopes the issue resolves itself during
    one attempt to parse.

    This will wait the following:
        - Attempt 1 - 20s following failure
        - Attempt 2 - 40s following failure
        - Attempt 3 - 80s following failure

    """
    result = None
    succeeded = False
    retry_time = 20.0
    retry_count = 0
    status_codes = []
    max_retry_count = 3

    while retry_count < max_retry_count and not succeeded:
        try:
            result = method_or_callable(*args)

            succeeded = True
        except ParseError as e:  # pragma: no cover
            cause_exec = e.__cause__
            if cause_exec is not None and isinstance(cause_exec, httpx.HTTPStatusError):
                status_codes.append(cause_exec.response.status_code)
                warnings.warn(
                    f"HTTP Exception for {cause_exec.request.url} - {cause_exec}",
                )
            else:
                warnings.warn(f"Unexpected error: {e}")
        except Exception as e:  # pragma: no cover
            warnings.warn(f"Unexpected error: {e}")

        retry_count = retry_count + 1

        time.sleep(retry_time)
        retry_time = retry_time * 2.0

    if (
        not succeeded
        and status_codes
        and skip_on_50x_err
        and all(httpx.codes.is_server_error(code) for code in status_codes)
    ):
        pytest.skip("Repeated HTTP 50x for service")  # pragma: no cover

    return succeeded, result


class DirectoriesMixin:
    def setUp(self) -> None:
        self.dirs = PaperlessDirectories(
            data_dir=Path(tempfile.mkdtemp()),
            scratch_dir=Path(tempfile.mkdtemp()),
            media_dir=Path(tempfile.mkdtemp()),
            consumption_dir=Path(tempfile.mkdtemp()),
            static_dir=Path(tempfile.mkdtemp()),
        )
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self.dirs.cleanup()


class FileSystemAssertsMixin:
    def assertIsFile(self, path: Union[PathLike, str]):
        self.assertTrue(Path(path).resolve().is_file(), f"File does not exist: {path}")

    def assertIsNotFile(self, path: Union[PathLike, str]):
        self.assertFalse(Path(path).resolve().is_file(), f"File does exist: {path}")

    def assertIsDir(self, path: Union[PathLike, str]):
        self.assertTrue(Path(path).resolve().is_dir(), f"Dir does not exist: {path}")

    def assertIsNotDir(self, path: Union[PathLike, str]):
        self.assertFalse(Path(path).resolve().is_dir(), f"Dir does exist: {path}")

    def assertFilesEqual(
        self,
        path1: Union[PathLike, str],
        path2: Union[PathLike, str],
    ):
        path1 = Path(path1)
        path2 = Path(path2)
        import hashlib

        hash1 = hashlib.sha256(path1.read_bytes()).hexdigest()
        hash2 = hashlib.sha256(path2.read_bytes()).hexdigest()

        self.assertEqual(hash1, hash2, "File SHA256 mismatch")


class ConsumerProgressMixin:
    def setUp(self) -> None:
        self.send_progress_patcher = mock.patch(
            "documents.consumer.Consumer._send_progress",
        )
        self.send_progress_mock = self.send_progress_patcher.start()
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self.send_progress_patcher.stop()


class DocumentConsumeDelayMixin:
    """
    Provides mocking of the consume_file asynchronous task and useful utilities
    for decoding its arguments
    """

    def setUp(self) -> None:
        self.consume_file_patcher = mock.patch("documents.tasks.consume_file.delay")
        self.consume_file_mock = self.consume_file_patcher.start()
        super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self.consume_file_patcher.stop()

    def get_last_consume_delay_call_args(
        self,
    ) -> tuple[ConsumableDocument, DocumentMetadataOverrides]:
        """
        Returns the most recent arguments to the async task
        """
        # Must be at least 1 call
        self.consume_file_mock.assert_called()

        args, _ = self.consume_file_mock.call_args
        input_doc, overrides = args

        return (input_doc, overrides)

    def get_all_consume_delay_call_args(
        self,
    ) -> Iterator[tuple[ConsumableDocument, DocumentMetadataOverrides]]:
        """
        Iterates over all calls to the async task and returns the arguments
        """

        for args, _ in self.consume_file_mock.call_args_list:
            input_doc, overrides = args

            yield (input_doc, overrides)

    def get_specific_consume_delay_call_args(
        self,
        index: int,
    ) -> Iterator[tuple[ConsumableDocument, DocumentMetadataOverrides]]:
        """
        Returns the arguments of a specific call to the async task
        """
        # Must be at least 1 call
        self.consume_file_mock.assert_called()

        args, _ = self.consume_file_mock.call_args_list[index]
        input_doc, overrides = args

        return (input_doc, overrides)


class TestMigrations(TransactionTestCase):
    @property
    def app(self):
        return apps.get_containing_app_config(type(self).__module__).name

    migrate_from = None
    migrate_to = None
    auto_migrate = True

    def setUp(self):
        super().setUp()

        assert (
            self.migrate_from and self.migrate_to
        ), "TestCase '{}' must define migrate_from and migrate_to properties".format(
            type(self).__name__,
        )
        self.migrate_from = [(self.app, self.migrate_from)]
        self.migrate_to = [(self.app, self.migrate_to)]
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        # Reverse to the original migration
        executor.migrate(self.migrate_from)

        self.setUpBeforeMigration(old_apps)

        self.apps = old_apps

        if self.auto_migrate:
            self.performMigration()

    def performMigration(self):
        # Run the migration to test
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()  # reload.
        executor.migrate(self.migrate_to)

        self.apps = executor.loader.project_state(self.migrate_to).apps

    def setUpBeforeMigration(self, apps):
        pass
