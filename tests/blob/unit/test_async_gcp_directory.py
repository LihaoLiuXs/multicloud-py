"""Unit tests for GcpAsyncBlobStore directory upload/download with transfer_manager.

Tests validate the GCP async provider correctly:
- Collects files from local directory (flat and recursive)
- Delegates uploads to transfer_manager.upload_many_from_filenames
- Delegates downloads to transfer_manager.download_many_to_path
- Applies prefix to GCS blob names
- Encodes tags as gcp-tag-* metadata entries via blob_constructor_kwargs
- Handles individual transfer failures (continue-on-error)
- Respects prefix exclusions during download
- Handles path traversal via transfer_manager's built-in protection
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.cloud import storage as sync_storage

pytest.importorskip(
    "gcloud.aio.storage",
    reason="gcloud-aio-storage not installed (optional async-gcp dependency)",
)

from multicloud.blob.blob_client.async_blob.async_driver import (
    AsyncDirectoryDownloadResponse,
    AsyncDirectoryUploadResponse,
    FailedAsyncBlobDownload,
    FailedAsyncBlobUpload,
)
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import (
    _GCP_TAG_PREFIX,
    GcpAsyncBlobStore,
)


def _make_mock_storage():
    """Create a mock gcloud.aio.storage.Storage object."""
    mock_storage = AsyncMock()
    mock_storage.upload.return_value = {}
    mock_storage.download.return_value = b"hello"
    mock_storage.list_objects.return_value = {"items": []}
    return mock_storage


def _make_mock_session():
    """Create a mock aiohttp.ClientSession."""
    mock_session = AsyncMock()
    return mock_session


@pytest.fixture
def mock_storage():
    return _make_mock_storage()


@pytest.fixture
def gcp_store(mock_storage, mock_session):
    """GcpAsyncBlobStore with pre-injected mock storage and session."""
    store = GcpAsyncBlobStore.__new__(GcpAsyncBlobStore)
    store._bucket = "test-bucket"
    store._region = "us-central1"
    store._endpoint_url = None
    store._transfer_config = None
    store._storage = mock_storage
    store._session = _make_mock_session()
    # Set by __init__, which this fixture bypasses.
    store._proxy_config = None
    store._owned_session = None
    return store


@pytest.fixture
def mock_session():
    return _make_mock_session()


@pytest.fixture
def tmp_upload_dir():
    """Create a temporary directory with test files."""
    tmp = tempfile.mkdtemp(prefix="test_gcp_upload_")
    d = Path(tmp)
    (d / "file1.txt").write_text("hello")
    (d / "file2.txt").write_text("world!")
    sub = d / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content")
    yield d

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def empty_dir():
    """Create an empty temporary directory."""
    tmp = tempfile.mkdtemp(prefix="test_gcp_empty_")
    yield Path(tmp)

    shutil.rmtree(tmp, ignore_errors=True)


_TM_MODULE = "multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store.transfer_manager"
_SYNC_STORAGE_MODULE = (
    "multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store.sync_storage"
)


class TestGcpAsyncUploadDirectory:
    """Tests for GcpAsyncBlobStore.do_upload_directory()."""

    @pytest.mark.asyncio
    async def test_upload_directory_all_files(self, gcp_store, tmp_upload_dir):
        """Uploads all files including subdirectories."""
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_bucket = MagicMock()
            mock_client_cls.return_value.bucket.return_value = mock_bucket
            mock_upload.return_value = [None, None, None]

            result = await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="data/",
                include_sub_folders=True,
            )

        assert isinstance(result, AsyncDirectoryUploadResponse)
        assert result.total_files == 3
        assert result.successful_uploads == 3
        assert result.has_failures is False
        assert result.total_bytes_transferred > 0
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args
        assert call_kwargs[1]["blob_name_prefix"] == "data/"
        assert call_kwargs[1]["max_workers"] == 10

    @pytest.mark.asyncio
    async def test_upload_directory_flat_only(self, gcp_store, tmp_upload_dir):
        """Uploads only top-level files when include_sub_folders=False."""
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_client_cls.return_value.bucket.return_value = MagicMock()
            mock_upload.return_value = [None, None]

            result = await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="flat/",
                include_sub_folders=False,
            )

        assert result.total_files == 2
        assert result.successful_uploads == 2
        assert result.has_failures is False

    @pytest.mark.asyncio
    async def test_upload_directory_with_prefix(self, gcp_store, tmp_upload_dir):
        """Prefix is passed as blob_name_prefix to transfer_manager."""
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_client_cls.return_value.bucket.return_value = MagicMock()
            mock_upload.return_value = [None, None]

            await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="uploads/v1/",
                include_sub_folders=False,
            )

        call_kwargs = mock_upload.call_args
        assert call_kwargs[1]["blob_name_prefix"] == "uploads/v1/"

    @pytest.mark.asyncio
    async def test_upload_directory_with_tags(self, gcp_store, tmp_upload_dir):
        """Tags travel as additional_blob_attributes metadata.

        They must be *attributes* set on each blob after construction, not
        constructor arguments: ``Bucket.blob()`` has no ``metadata`` parameter,
        so routing them the other way raises TypeError for every file.
        """
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_client_cls.return_value.bucket.return_value = MagicMock()
            mock_upload.return_value = [None, None]

            await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="",
                include_sub_folders=False,
                tags={"env": "test", "team": "multicloud"},
            )

        call_kwargs = mock_upload.call_args[1]
        attributes = call_kwargs["additional_blob_attributes"]
        assert attributes["metadata"] == {
            f"{_GCP_TAG_PREFIX}env": "test",
            f"{_GCP_TAG_PREFIX}team": "multicloud",
        }
        assert "metadata" not in call_kwargs.get("blob_constructor_kwargs", {})

    @pytest.mark.asyncio
    async def test_upload_directory_blob_constructor_kwargs_are_accepted_by_blob(
        self, gcp_store, tmp_upload_dir
    ):
        """Whatever is routed to the constructor must be a Bucket.blob() parameter.

        transfer_manager forwards ``blob_constructor_kwargs`` straight into
        ``Bucket.blob()``, so an argument that method does not accept fails every
        file in the upload -- and mocking transfer_manager hides it, since the
        real constructor is never called. Checking against the live signature is
        what catches it.
        """
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_client_cls.return_value.bucket.return_value = MagicMock()
            mock_upload.return_value = [None, None]

            await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="",
                include_sub_folders=False,
                tags={"env": "test"},
            )

        passed = set(mock_upload.call_args[1].get("blob_constructor_kwargs") or {})
        accepted = set(inspect.signature(sync_storage.Bucket.blob).parameters) - {
            "self"
        }
        assert passed <= accepted, f"not Bucket.blob() parameters: {passed - accepted}"

    @pytest.mark.asyncio
    async def test_upload_directory_empty(self, gcp_store, empty_dir):
        """Returns zero-count response for empty directory."""
        result = await gcp_store.upload_directory(
            str(empty_dir),
            prefix="empty/",
        )

        assert result.total_files == 0
        assert result.successful_uploads == 0
        assert result.total_bytes_transferred == 0
        assert result.has_failures is False

    @pytest.mark.asyncio
    async def test_upload_directory_partial_failure(self, gcp_store, tmp_upload_dir):
        """Individual failures tracked without aborting batch."""
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_client_cls.return_value.bucket.return_value = MagicMock()
            mock_upload.return_value = [
                None,
                RuntimeError("Simulated GCS failure"),
                None,
            ]

            result = await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="",
                include_sub_folders=True,
            )

        assert result.total_files == 3
        assert result.successful_uploads == 2
        assert len(result.failed_transfers) == 1
        assert result.has_failures is True
        assert isinstance(result.failed_transfers[0], FailedAsyncBlobUpload)

    @pytest.mark.asyncio
    async def test_upload_directory_concurrency_bounded(
        self, gcp_store, tmp_upload_dir
    ):
        """Max concurrency is passed to transfer_manager as max_workers."""
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.upload_many_from_filenames") as mock_upload,
        ):
            mock_client_cls.return_value.bucket.return_value = MagicMock()
            mock_upload.return_value = [None, None, None]

            result = await gcp_store.upload_directory(
                str(tmp_upload_dir),
                prefix="",
                include_sub_folders=True,
                max_concurrency=1,
            )

        assert result.total_files == 3
        assert result.successful_uploads == 3
        call_kwargs = mock_upload.call_args
        assert call_kwargs[1]["max_workers"] == 1

    @pytest.mark.asyncio
    async def test_upload_directory_validates_not_exists(self, gcp_store):
        """Raises InvalidArgumentException for nonexistent directory."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="does not exist"):
            await gcp_store.upload_directory(
                "/nonexistent/path/xyz",
                prefix="",
            )

    @pytest.mark.asyncio
    async def test_upload_directory_validates_not_a_directory(
        self, gcp_store, tmp_upload_dir
    ):
        """Raises InvalidArgumentException when path is a file."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="not a directory"):
            await gcp_store.upload_directory(
                str(tmp_upload_dir / "file1.txt"),
                prefix="",
            )

    @pytest.mark.asyncio
    async def test_upload_directory_validates_empty_path(self, gcp_store):
        """Raises InvalidArgumentException for empty source path."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="required"):
            await gcp_store.upload_directory(
                "",
                prefix="",
            )


class TestGcpAsyncDownloadDirectory:
    """Tests for GcpAsyncBlobStore.do_download_directory()."""

    @pytest.fixture
    def download_dest(self):
        tmp = tempfile.mkdtemp(prefix="test_gcp_download_")
        yield Path(tmp)

        shutil.rmtree(tmp, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_download_directory_basic(self, gcp_store, download_dest):
        """Downloads all listed objects to local directory."""
        mock_blob1 = MagicMock(name="data/file1.txt", size=5)
        mock_blob1.name = "data/file1.txt"
        mock_blob2 = MagicMock(name="data/file2.txt", size=6)
        mock_blob2.name = "data/file2.txt"

        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = [mock_blob1, mock_blob2]

            # Simulate successful downloads by creating files
            def download_side_effect(bucket, blob_names, **kwargs):
                dest = kwargs.get("destination_directory", "")
                prefix = kwargs.get("blob_name_prefix", "")
                for name in blob_names:
                    relative = name[len(prefix) :] if name.startswith(prefix) else name
                    path = Path(dest) / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"hello")
                return [None] * len(blob_names)

            mock_download.side_effect = download_side_effect

            result = await gcp_store.download_directory(
                "data",
                str(download_dest),
            )

        assert isinstance(result, AsyncDirectoryDownloadResponse)
        assert result.total_files == 2
        assert result.successful_downloads == 2
        assert result.has_failures is False
        assert result.total_bytes_transferred == 10

    @pytest.mark.asyncio
    async def test_download_directory_creates_parent_dirs(
        self, gcp_store, download_dest
    ):
        """Creates nested parent directories for downloaded files."""
        mock_blob = MagicMock()
        mock_blob.name = "prefix/a/b/deep.txt"
        mock_blob.size = 4

        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = [mock_blob]

            def download_side_effect(bucket, blob_names, **kwargs):
                dest = kwargs.get("destination_directory", "")
                prefix = kwargs.get("blob_name_prefix", "")
                for name in blob_names:
                    relative = name[len(prefix) :] if name.startswith(prefix) else name
                    path = Path(dest) / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"data")
                return [None] * len(blob_names)

            mock_download.side_effect = download_side_effect

            result = await gcp_store.download_directory(
                "prefix",
                str(download_dest),
            )

        assert result.successful_downloads == 1
        expected_file = download_dest / "a" / "b" / "deep.txt"
        assert expected_file.exists()
        assert expected_file.read_text() == "data"

    @pytest.mark.asyncio
    async def test_download_directory_skips_dir_markers(self, gcp_store, download_dest):
        """Skips GCS directory markers (names ending with / and size 0)."""
        mock_dir = MagicMock()
        mock_dir.name = "data/"
        mock_dir.size = 0
        mock_real = MagicMock()
        mock_real.name = "data/real.txt"
        mock_real.size = 4

        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = [mock_dir, mock_real]

            def download_side_effect(bucket, blob_names, **kwargs):
                dest = kwargs.get("destination_directory", "")
                prefix = kwargs.get("blob_name_prefix", "")
                for name in blob_names:
                    relative = name[len(prefix) :] if name.startswith(prefix) else name
                    path = Path(dest) / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"data")
                return [None] * len(blob_names)

            mock_download.side_effect = download_side_effect

            result = await gcp_store.download_directory(
                "data",
                str(download_dest),
            )

        assert result.total_files == 1
        assert result.successful_downloads == 1

    @pytest.mark.asyncio
    async def test_download_directory_with_exclusions(self, gcp_store, download_dest):
        """Respects prefixes_to_exclude."""
        mock_blobs = []
        for name in ["data/keep/a.txt", "data/skip/b.txt", "data/keep/c.txt"]:
            blob = MagicMock()
            blob.name = name
            blob.size = 5
            mock_blobs.append(blob)

        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = mock_blobs

            def download_side_effect(bucket, blob_names, **kwargs):
                dest = kwargs.get("destination_directory", "")
                prefix = kwargs.get("blob_name_prefix", "")
                for name in blob_names:
                    relative = name[len(prefix) :] if name.startswith(prefix) else name
                    path = Path(dest) / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"hello")
                return [None] * len(blob_names)

            mock_download.side_effect = download_side_effect

            result = await gcp_store.download_directory(
                "data",
                str(download_dest),
                prefixes_to_exclude=["data/skip/"],
            )

        assert result.total_files == 2
        assert result.successful_downloads == 2

    @pytest.mark.asyncio
    async def test_download_directory_path_traversal_protection(
        self, gcp_store, download_dest
    ):
        """Transfer_manager returns UserWarning for path traversal attempts."""
        mock_blobs = []
        for name, size in [("data/../../../etc/passwd", 100), ("data/safe.txt", 4)]:
            blob = MagicMock()
            blob.name = name
            blob.size = size
            mock_blobs.append(blob)

        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = mock_blobs

            def download_side_effect(bucket, blob_names, **kwargs):
                dest = kwargs.get("destination_directory", "")
                prefix = kwargs.get("blob_name_prefix", "")
                results = []
                for name in blob_names:
                    if ".." in name:
                        results.append(UserWarning(f"Path traversal detected: {name}"))
                    else:
                        relative = (
                            name[len(prefix) :] if name.startswith(prefix) else name
                        )
                        path = Path(dest) / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"safe")
                        results.append(None)
                return results

            mock_download.side_effect = download_side_effect

            result = await gcp_store.download_directory(
                "data",
                str(download_dest),
            )

        assert result.total_files == 2
        assert result.successful_downloads == 1
        assert len(result.failed_transfers) == 1

    @pytest.mark.asyncio
    async def test_download_directory_empty_listing(self, gcp_store, download_dest):
        """Returns zero-count response when no objects found."""
        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = []

            result = await gcp_store.download_directory(
                "empty",
                str(download_dest),
            )

        assert result.total_files == 0
        assert result.successful_downloads == 0
        assert result.total_bytes_transferred == 0
        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_directory_partial_failure(self, gcp_store, download_dest):
        """Individual download failures tracked without abort."""
        mock_blobs = []
        for name in ["data/ok.txt", "data/bad.txt"]:
            blob = MagicMock()
            blob.name = name
            blob.size = 5
            mock_blobs.append(blob)

        with (
            patch(f"{_SYNC_STORAGE_MODULE}.Client") as mock_client_cls,
            patch(f"{_TM_MODULE}.download_many_to_path") as mock_download,
        ):
            mock_client = mock_client_cls.return_value
            mock_client.bucket.return_value = MagicMock()
            mock_client.list_blobs.return_value = mock_blobs

            def download_side_effect(bucket, blob_names, **kwargs):
                dest = kwargs.get("destination_directory", "")
                prefix = kwargs.get("blob_name_prefix", "")
                results = []
                for name in blob_names:
                    if "bad" in name:
                        results.append(RuntimeError("Download error"))
                    else:
                        relative = (
                            name[len(prefix) :] if name.startswith(prefix) else name
                        )
                        path = Path(dest) / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"hello")
                        results.append(None)
                return results

            mock_download.side_effect = download_side_effect

            result = await gcp_store.download_directory(
                "data",
                str(download_dest),
            )

        assert result.total_files == 2
        assert result.successful_downloads == 1
        assert len(result.failed_transfers) == 1
        assert result.has_failures is True

    @pytest.mark.asyncio
    async def test_download_directory_validates_empty_prefix(self, gcp_store):
        """Raises InvalidArgumentException for empty prefix."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="required"):
            await gcp_store.download_directory(
                "",
                "/tmp/dest",
            )

    @pytest.mark.asyncio
    async def test_download_directory_validates_empty_destination(self, gcp_store):
        """Raises InvalidArgumentException for empty destination."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="required"):
            await gcp_store.download_directory(
                "prefix",
                "",
            )


def _make_response_error(
    status: int = 404, message: str = "Not Found"
) -> "aiohttp.ClientResponseError":
    """Create an aiohttp ClientResponseError for testing."""
    import aiohttp

    mock_request_info = MagicMock()
    mock_request_info.real_url = "https://storage.googleapis.com/test"
    return aiohttp.ClientResponseError(
        request_info=mock_request_info,
        history=(),
        status=status,
        message=message,
    )


def _list_page(names: List[str], *, next_token: str | None = None) -> dict:
    """Build a list_objects response page."""
    page: dict[str, Any] = {"items": [{"name": n} for n in names]}
    if next_token is not None:
        page["nextPageToken"] = next_token
    return page


class TestGcpAsyncDeleteDirectory:
    """Tests for GcpAsyncBlobStore.do_delete_directory()."""

    @pytest.mark.asyncio
    async def test_deletes_every_listed_key(self, gcp_store, mock_storage):
        mock_storage.list_objects.return_value = _list_page(
            ["data/a.txt", "data/sub/b.txt"]
        )

        await gcp_store.delete_directory("data/")

        deleted = [call.args[1] for call in mock_storage.delete.call_args_list]
        assert sorted(deleted) == ["data/a.txt", "data/sub/b.txt"]
        for call in mock_storage.delete.call_args_list:
            assert call.args[0] == "test-bucket"

    @pytest.mark.asyncio
    async def test_lists_with_prefix_and_without_delimiter(
        self, gcp_store, mock_storage
    ):
        """A delimiter would hide nested keys, making the delete non-recursive."""
        mock_storage.list_objects.return_value = _list_page(["data/a.txt"])

        await gcp_store.delete_directory("data/")

        params = mock_storage.list_objects.call_args.kwargs["params"]
        assert params["prefix"] == "data/"
        assert "delimiter" not in params

    @pytest.mark.asyncio
    async def test_empty_prefix_omits_prefix_and_targets_whole_bucket(
        self, gcp_store, mock_storage
    ):
        mock_storage.list_objects.return_value = _list_page(["top.txt"])

        await gcp_store.delete_directory("")

        assert "prefix" not in mock_storage.list_objects.call_args.kwargs["params"]

    @pytest.mark.asyncio
    async def test_none_prefix_omits_prefix(self, gcp_store, mock_storage):
        mock_storage.list_objects.return_value = _list_page(["top.txt"])

        await gcp_store.delete_directory(None)

        assert "prefix" not in mock_storage.list_objects.call_args.kwargs["params"]

    @pytest.mark.asyncio
    async def test_nothing_matched_is_a_no_op(self, gcp_store, mock_storage):
        mock_storage.list_objects.return_value = {"items": []}

        await gcp_store.delete_directory("empty/")

        mock_storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_pagination_is_followed_to_exhaustion(self, gcp_store, mock_storage):
        """A single page caps at 1000 items, so a larger directory needs paging."""
        mock_storage.list_objects.side_effect = [
            _list_page(["data/a.txt"], next_token="token-1"),
            _list_page(["data/b.txt"]),
        ]

        await gcp_store.delete_directory("data/")

        assert mock_storage.list_objects.call_count == 2
        second_params = mock_storage.list_objects.call_args_list[1].kwargs["params"]
        assert second_params["pageToken"] == "token-1"
        deleted = [call.args[1] for call in mock_storage.delete.call_args_list]
        assert sorted(deleted) == ["data/a.txt", "data/b.txt"]

    @pytest.mark.asyncio
    async def test_directory_marker_objects_are_deleted(self, gcp_store, mock_storage):
        """Unlike download, markers are real objects that must be removed."""
        mock_storage.list_objects.return_value = _list_page(["data/", "data/a.txt"])

        await gcp_store.delete_directory("data/")

        deleted = [call.args[1] for call in mock_storage.delete.call_args_list]
        assert "data/" in deleted

    @pytest.mark.asyncio
    async def test_items_without_a_name_are_skipped(self, gcp_store, mock_storage):
        mock_storage.list_objects.return_value = {
            "items": [{"name": "data/a.txt"}, {}, {"name": ""}]
        }

        await gcp_store.delete_directory("data/")

        deleted = [call.args[1] for call in mock_storage.delete.call_args_list]
        assert deleted == ["data/a.txt"]

    @pytest.mark.asyncio
    async def test_missing_object_counts_as_deleted(self, gcp_store, mock_storage):
        """404 is success, matching the idempotent single-object delete."""
        mock_storage.list_objects.return_value = _list_page(["data/gone.txt"])
        mock_storage.delete.side_effect = _make_response_error(404, "Not Found")

        await gcp_store.delete_directory("data/")

        assert mock_storage.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_failed_delete_is_surfaced(self, gcp_store, mock_storage):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobAsyncError

        mock_storage.list_objects.return_value = _list_page(["data/locked.txt"])
        mock_storage.delete.side_effect = _make_response_error(403, "Forbidden")

        with pytest.raises(BlobAsyncError) as exc_info:
            await gcp_store.delete_directory("data/")

        message = str(exc_info.value)
        assert "failed to delete 1 object(s)" in message
        assert "data/locked.txt" in message
        assert "403" in message

    @pytest.mark.asyncio
    async def test_failed_delete_preserves_the_underlying_error(
        self, gcp_store, mock_storage
    ):
        """The original error must survive so callers can inspect the status.

        Stringifying it here would strand the aggregate with cause=None.
        """
        import aiohttp
        from multicloud.blob.blob_client.async_blob.async_driver import BlobAsyncError

        mock_storage.list_objects.return_value = _list_page(["data/locked.txt"])
        mock_storage.delete.side_effect = _make_response_error(403, "Forbidden")

        with pytest.raises(BlobAsyncError) as exc_info:
            await gcp_store.delete_directory("data/")

        cause = exc_info.value.cause
        assert isinstance(cause, BlobAsyncError)
        assert isinstance(cause.cause, aiohttp.ClientResponseError)
        assert cause.cause.status == 403

    @pytest.mark.asyncio
    async def test_network_fault_names_the_failing_object(
        self, gcp_store, mock_storage
    ):
        """A transport fault must still identify which object failed."""
        import aiohttp
        from multicloud.blob.blob_client.async_blob.async_driver import BlobAsyncError

        mock_storage.list_objects.return_value = _list_page(["data/flaky.txt"])
        mock_storage.delete.side_effect = aiohttp.ClientError("Server disconnected")

        with pytest.raises(BlobAsyncError) as exc_info:
            await gcp_store.delete_directory("data/")

        message = str(exc_info.value)
        assert "data/flaky.txt" in message
        assert "Server disconnected" in message

    @pytest.mark.asyncio
    async def test_partial_failure_still_attempts_every_key(
        self, gcp_store, mock_storage
    ):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobAsyncError

        mock_storage.list_objects.return_value = _list_page(
            ["data/a.txt", "data/b.txt", "data/c.txt"]
        )

        async def delete_side_effect(bucket, key, **kwargs):
            if key == "data/b.txt":
                raise _make_response_error(403, "Forbidden")

        mock_storage.delete.side_effect = delete_side_effect

        with pytest.raises(BlobAsyncError) as exc_info:
            await gcp_store.delete_directory("data/")

        assert "failed to delete 1 object(s)" in str(exc_info.value)
        assert mock_storage.delete.call_count == 3

    @pytest.mark.asyncio
    async def test_max_concurrency_is_validated(self, gcp_store):
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="max_concurrency"):
            await gcp_store.delete_directory("data/", max_concurrency=0)

    @pytest.mark.asyncio
    async def test_uninitialized_storage_raises(self, gcp_store):
        gcp_store._storage = None
        gcp_store._client_lock = asyncio.Lock()
        gcp_store._service_file = None
        gcp_store._credentials_overrider = None

        with patch(
            "multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store.Storage",
            return_value=None,
        ):
            with pytest.raises(RuntimeError, match="not initialized"):
                await gcp_store.delete_directory("data/")
