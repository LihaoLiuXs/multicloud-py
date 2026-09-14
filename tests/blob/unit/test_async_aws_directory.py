"""Unit tests for AwsAsyncBlobStore directory upload/download with mocked aiobotocore.

Tests validate the AWS async provider correctly:
- Collects files from local directory (flat and recursive)
- Uploads with bounded concurrency via asyncio.Semaphore
- Applies prefix to S3 keys
- URL-encodes tags into Tagging header
- Handles individual upload failures (continue-on-error)
- Lists objects with pagination
- Downloads concurrently with path traversal protection
- Respects prefix exclusions during download
- Creates parent directories on download
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip(
    "aiobotocore", reason="aiobotocore not installed (optional async-aws dependency)"
)

from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    AsyncDirectoryDownloadResponse,
    AsyncDirectoryUploadResponse,
    FailedAsyncBlobDownload,
    FailedAsyncBlobUpload,
)


def _make_mock_session():
    """Create a mock aiobotocore session with a mock S3 client."""
    mock_client = AsyncMock()
    mock_client.put_object.return_value = {"ETag": '"abc123"', "VersionId": "v1"}

    mock_client_cm = AsyncMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.create_client.return_value = mock_client_cm

    return mock_session, mock_client


@pytest.fixture
def mock_session():
    return _make_mock_session()


@pytest.fixture
def aws_store(mock_session):
    session, _ = mock_session
    return AwsAsyncBlobStore(
        bucket="test-bucket",
        region="us-east-1",
        _session=session,
    )


@pytest.fixture
def mock_client(mock_session):
    _, client = mock_session
    return client


@pytest.fixture
def tmp_upload_dir():
    """Create a temporary directory with test files."""
    tmp = tempfile.mkdtemp(prefix="test_upload_")
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
    tmp = tempfile.mkdtemp(prefix="test_empty_")
    yield Path(tmp)

    shutil.rmtree(tmp, ignore_errors=True)


class TestAwsAsyncUploadDirectory:
    """Tests for AwsAsyncBlobStore.do_upload_directory()."""

    @pytest.mark.asyncio
    async def test_upload_directory_all_files(
        self, aws_store, mock_client, tmp_upload_dir
    ):
        """Uploads all files including subdirectories."""
        result = await aws_store.upload_directory(
            str(tmp_upload_dir),
            prefix="data/",
            include_sub_folders=True,
        )

        assert isinstance(result, AsyncDirectoryUploadResponse)
        assert result.total_files == 3
        assert result.successful_uploads == 3
        assert result.has_failures is False
        assert result.total_bytes_transferred > 0
        assert mock_client.put_object.call_count == 3

    @pytest.mark.asyncio
    async def test_upload_directory_flat_only(
        self, aws_store, mock_client, tmp_upload_dir
    ):
        """Uploads only top-level files when include_sub_folders=False."""
        result = await aws_store.upload_directory(
            str(tmp_upload_dir),
            prefix="flat/",
            include_sub_folders=False,
        )

        assert result.total_files == 2
        assert result.successful_uploads == 2
        assert result.has_failures is False

    @pytest.mark.asyncio
    async def test_upload_directory_with_prefix(
        self, aws_store, mock_client, tmp_upload_dir
    ):
        """Prefix is prepended to S3 keys."""
        await aws_store.upload_directory(
            str(tmp_upload_dir),
            prefix="uploads/v1/",
            include_sub_folders=False,
        )

        calls = mock_client.put_object.call_args_list
        keys = [c.kwargs["Key"] for c in calls]
        assert all(k.startswith("uploads/v1/") for k in keys)

    @pytest.mark.asyncio
    async def test_upload_directory_with_tags(
        self, aws_store, mock_client, tmp_upload_dir
    ):
        """Tags are URL-encoded in the Tagging header."""
        await aws_store.upload_directory(
            str(tmp_upload_dir),
            prefix="",
            include_sub_folders=False,
            tags={"env": "test", "team": "multicloud"},
        )

        calls = mock_client.put_object.call_args_list
        for c in calls:
            tagging = c.kwargs["Tagging"]
            assert "env=test" in tagging
            assert "team=multicloud" in tagging

    @pytest.mark.asyncio
    async def test_upload_directory_empty(self, aws_store, mock_client, empty_dir):
        """Returns zero-count response for empty directory."""
        result = await aws_store.upload_directory(
            str(empty_dir),
            prefix="empty/",
        )

        assert result.total_files == 0
        assert result.successful_uploads == 0
        assert result.total_bytes_transferred == 0
        assert result.has_failures is False
        mock_client.put_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_directory_partial_failure(
        self, aws_store, mock_client, tmp_upload_dir
    ):
        """Individual failures tracked without aborting batch."""
        call_count = [0]

        async def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Simulated S3 failure")
            return {"ETag": '"x"', "VersionId": "v"}

        mock_client.put_object.side_effect = side_effect

        result = await aws_store.upload_directory(
            str(tmp_upload_dir),
            prefix="",
            include_sub_folders=True,
        )

        assert result.total_files == 3
        assert result.successful_uploads == 2
        assert len(result.failed_transfers) == 1
        assert result.has_failures is True
        assert isinstance(result.failed_transfers[0], FailedAsyncBlobUpload)
        assert isinstance(result.failed_transfers[0].exception, RuntimeError)

    @pytest.mark.asyncio
    async def test_upload_directory_concurrency_bounded(
        self, aws_store, mock_client, tmp_upload_dir
    ):
        """Max concurrency is respected (Semaphore used)."""
        result = await aws_store.upload_directory(
            str(tmp_upload_dir),
            prefix="",
            include_sub_folders=True,
            max_concurrency=1,  # serialize uploads
        )

        # All should succeed even with max_concurrency=1
        assert result.total_files == 3
        assert result.successful_uploads == 3

    @pytest.mark.asyncio
    async def test_upload_directory_validates_not_exists(self, aws_store):
        """Raises InvalidArgumentException for nonexistent directory."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="does not exist"):
            await aws_store.upload_directory(
                "/nonexistent/path/xyz",
                prefix="",
            )

    @pytest.mark.asyncio
    async def test_upload_directory_validates_not_a_directory(
        self, aws_store, tmp_upload_dir
    ):
        """Raises InvalidArgumentException when path is a file, not directory."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="not a directory"):
            await aws_store.upload_directory(
                str(tmp_upload_dir / "file1.txt"),
                prefix="",
            )

    @pytest.mark.asyncio
    async def test_upload_directory_validates_empty_path(self, aws_store):
        """Raises InvalidArgumentException for empty source path."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="required"):
            await aws_store.upload_directory(
                "",
                prefix="",
            )


class TestAwsAsyncDownloadDirectory:
    """Tests for AwsAsyncBlobStore.do_download_directory()."""

    @pytest.fixture
    def download_dest(self):
        tmp = tempfile.mkdtemp(prefix="test_download_")
        yield Path(tmp)

        shutil.rmtree(tmp, ignore_errors=True)

    def _list_response(self, contents, is_truncated=False, token=None):
        resp = {
            "Contents": contents,
            "IsTruncated": is_truncated,
        }
        if token:
            resp["NextContinuationToken"] = token
        return resp

    @pytest.mark.asyncio
    async def test_download_directory_basic(
        self, aws_store, mock_client, download_dest
    ):
        """Downloads all listed objects to local directory."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "data/file1.txt", "Size": 5},
                {"Key": "data/file2.txt", "Size": 6},
            ]
        )

        body_mock = AsyncMock()
        body_mock.read.return_value = b"hello"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
        )

        assert isinstance(result, AsyncDirectoryDownloadResponse)
        assert result.total_files == 2
        assert result.successful_downloads == 2
        assert result.has_failures is False
        assert result.total_bytes_transferred == 10  # 5 * 2

    @pytest.mark.asyncio
    async def test_download_directory_creates_parent_dirs(
        self, aws_store, mock_client, download_dest
    ):
        """Creates nested parent directories for downloaded files."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "prefix/a/b/deep.txt", "Size": 4},
            ]
        )

        body_mock = AsyncMock()
        body_mock.read.return_value = b"data"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "prefix",
            str(download_dest),
        )

        assert result.successful_downloads == 1
        expected_file = download_dest / "a" / "b" / "deep.txt"
        assert expected_file.exists()
        assert expected_file.read_text() == "data"

    @pytest.mark.asyncio
    async def test_download_directory_skips_dir_markers(
        self, aws_store, mock_client, download_dest
    ):
        """Skips S3 directory markers (keys ending with / and size 0)."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "data/", "Size": 0},  # directory marker
                {"Key": "data/real.txt", "Size": 4},
            ]
        )

        body_mock = AsyncMock()
        body_mock.read.return_value = b"data"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
        )

        assert result.total_files == 1
        assert result.successful_downloads == 1

    @pytest.mark.asyncio
    async def test_download_directory_with_exclusions(
        self, aws_store, mock_client, download_dest
    ):
        """Respects prefixes_to_exclude."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "data/keep/a.txt", "Size": 5},
                {"Key": "data/skip/b.txt", "Size": 5},
                {"Key": "data/keep/c.txt", "Size": 5},
            ]
        )

        body_mock = AsyncMock()
        body_mock.read.return_value = b"hello"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
            prefixes_to_exclude=["data/skip/"],
        )

        assert result.total_files == 2
        assert result.successful_downloads == 2

    @pytest.mark.asyncio
    async def test_download_directory_path_traversal_protection(
        self, aws_store, mock_client, download_dest
    ):
        """Rejects keys with '..' segments."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "data/../../../etc/passwd", "Size": 100},
                {"Key": "data/safe.txt", "Size": 4},
            ]
        )

        body_mock = AsyncMock()
        body_mock.read.return_value = b"safe"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
        )

        assert result.total_files == 2
        assert result.successful_downloads == 1
        assert len(result.failed_transfers) == 1
        assert "traversal" in str(result.failed_transfers[0].exception).lower()

    @pytest.mark.asyncio
    async def test_download_directory_pagination(
        self, aws_store, mock_client, download_dest
    ):
        """Handles paginated list_objects_v2 responses."""
        mock_client.list_objects_v2.side_effect = [
            self._list_response(
                [{"Key": "data/page1.txt", "Size": 5}],
                is_truncated=True,
                token="token-1",
            ),
            self._list_response(
                [{"Key": "data/page2.txt", "Size": 5}],
                is_truncated=False,
            ),
        ]

        body_mock = AsyncMock()
        body_mock.read.return_value = b"hello"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
        )

        assert result.total_files == 2
        assert result.successful_downloads == 2
        assert mock_client.list_objects_v2.call_count == 2

    @pytest.mark.asyncio
    async def test_download_directory_empty_listing(
        self, aws_store, mock_client, download_dest
    ):
        """Returns zero-count response when no objects found."""
        mock_client.list_objects_v2.return_value = self._list_response([])

        result = await aws_store.download_directory(
            "empty",
            str(download_dest),
        )

        assert result.total_files == 0
        assert result.successful_downloads == 0
        assert result.total_bytes_transferred == 0
        mock_client.get_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_directory_partial_failure(
        self, aws_store, mock_client, download_dest
    ):
        """Individual download failures tracked without abort."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "data/ok.txt", "Size": 5},
                {"Key": "data/bad.txt", "Size": 5},
            ]
        )

        call_count = [0]

        async def get_side_effect(**kwargs):
            call_count[0] += 1
            if kwargs.get("Key") == "data/bad.txt":
                raise RuntimeError("Download error")
            body_mock = AsyncMock()
            body_mock.read.return_value = b"hello"
            return {"Body": body_mock}

        mock_client.get_object.side_effect = get_side_effect

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
        )

        assert result.total_files == 2
        assert result.successful_downloads == 1
        assert len(result.failed_transfers) == 1
        assert result.has_failures is True

    @pytest.mark.asyncio
    async def test_download_directory_absolute_key_traversal(
        self, aws_store, mock_client, download_dest
    ):
        """Rejects absolute keys that would escape destination directory."""
        mock_client.list_objects_v2.return_value = self._list_response(
            [
                {"Key": "data//etc/passwd", "Size": 10},
                {"Key": "data/safe.txt", "Size": 4},
            ]
        )

        body_mock = AsyncMock()
        body_mock.read.return_value = b"safe"
        mock_client.get_object.return_value = {"Body": body_mock}

        result = await aws_store.download_directory(
            "data",
            str(download_dest),
        )

        assert result.total_files == 2
        assert result.successful_downloads == 1
        assert len(result.failed_transfers) == 1
        assert "traversal" in str(result.failed_transfers[0].exception).lower()

    @pytest.mark.asyncio
    async def test_download_directory_validates_empty_prefix(self, aws_store):
        """Raises InvalidArgumentException for empty prefix."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="prefix"):
            await aws_store.download_directory(
                "",
                "/tmp/test",
            )

    @pytest.mark.asyncio
    async def test_download_directory_validates_empty_destination(self, aws_store):
        """Raises InvalidArgumentException for empty destination."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="destination"):
            await aws_store.download_directory(
                "data",
                "",
            )

    @pytest.mark.asyncio
    async def test_download_directory_validates_max_concurrency(self, aws_store):
        """Raises InvalidArgumentException for max_concurrency <= 0."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="max_concurrency"):
            await aws_store.download_directory(
                "data",
                "/tmp/test",
                max_concurrency=0,
            )

    @pytest.mark.asyncio
    async def test_upload_directory_validates_max_concurrency(
        self, aws_store, tmp_upload_dir
    ):
        """Raises InvalidArgumentException for max_concurrency <= 0."""
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="max_concurrency"):
            await aws_store.upload_directory(
                str(tmp_upload_dir),
                prefix="",
                max_concurrency=-1,
            )


def _list_page(keys: List[str], *, next_token: str | None = None) -> dict:
    """Build a list_objects_v2 response page."""
    page: dict[str, Any] = {"Contents": [{"Key": k} for k in keys]}
    if next_token is not None:
        page["IsTruncated"] = True
        page["NextContinuationToken"] = next_token
    else:
        page["IsTruncated"] = False
    return page


class TestAwsAsyncDeleteDirectory:
    """Tests for AwsAsyncBlobStore.do_delete_directory()."""

    @pytest.mark.asyncio
    async def test_deletes_every_listed_key(self, aws_store, mock_client):
        mock_client.list_objects_v2.return_value = _list_page(
            ["data/a.txt", "data/sub/b.txt"]
        )
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("data/")

        assert mock_client.delete_objects.call_count == 1
        sent = mock_client.delete_objects.call_args.kwargs
        assert sent["Bucket"] == "test-bucket"
        assert sent["Delete"]["Objects"] == [
            {"Key": "data/a.txt"},
            {"Key": "data/sub/b.txt"},
        ]

    @pytest.mark.asyncio
    async def test_lists_with_prefix_and_without_delimiter(
        self, aws_store, mock_client
    ):
        """A delimiter would hide nested keys, making the delete non-recursive."""
        mock_client.list_objects_v2.return_value = _list_page(["data/a.txt"])
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("data/")

        list_kwargs = mock_client.list_objects_v2.call_args.kwargs
        assert list_kwargs["Prefix"] == "data/"
        assert "Delimiter" not in list_kwargs

    @pytest.mark.asyncio
    async def test_quiet_mode_is_enabled(self, aws_store, mock_client):
        """Quiet mode suppresses success entries but still returns errors."""
        mock_client.list_objects_v2.return_value = _list_page(["data/a.txt"])
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("data/")

        assert mock_client.delete_objects.call_args.kwargs["Delete"]["Quiet"] is True

    @pytest.mark.asyncio
    async def test_empty_prefix_omits_prefix_and_targets_whole_bucket(
        self, aws_store, mock_client
    ):
        mock_client.list_objects_v2.return_value = _list_page(["top.txt"])
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("")

        assert "Prefix" not in mock_client.list_objects_v2.call_args.kwargs

    @pytest.mark.asyncio
    async def test_none_prefix_omits_prefix(self, aws_store, mock_client):
        mock_client.list_objects_v2.return_value = _list_page(["top.txt"])
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory(None)

        assert "Prefix" not in mock_client.list_objects_v2.call_args.kwargs

    @pytest.mark.asyncio
    async def test_nothing_matched_is_a_no_op(self, aws_store, mock_client):
        mock_client.list_objects_v2.return_value = {"IsTruncated": False}

        await aws_store.delete_directory("empty/")

        mock_client.delete_objects.assert_not_called()

    @pytest.mark.asyncio
    async def test_pagination_is_followed_to_exhaustion(self, aws_store, mock_client):
        mock_client.list_objects_v2.side_effect = [
            _list_page(["data/a.txt"], next_token="token-1"),
            _list_page(["data/b.txt"]),
        ]
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("data/")

        assert mock_client.list_objects_v2.call_count == 2
        assert (
            mock_client.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"]
            == "token-1"
        )
        deleted = [
            obj["Key"]
            for call in mock_client.delete_objects.call_args_list
            for obj in call.kwargs["Delete"]["Objects"]
        ]
        assert sorted(deleted) == ["data/a.txt", "data/b.txt"]

    @pytest.mark.asyncio
    async def test_keys_are_chunked_at_the_batch_limit(self, aws_store, mock_client):
        """More than 1000 keys in one page must be split across requests."""
        keys = [f"data/{i}.txt" for i in range(1500)]
        mock_client.list_objects_v2.return_value = _list_page(keys)
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("data/")

        batch_sizes = [
            len(call.kwargs["Delete"]["Objects"])
            for call in mock_client.delete_objects.call_args_list
        ]
        assert batch_sizes == [1000, 500]

    @pytest.mark.asyncio
    async def test_directory_marker_objects_are_deleted(self, aws_store, mock_client):
        """Unlike download, markers are real objects that must be removed."""
        mock_client.list_objects_v2.return_value = _list_page(["data/", "data/a.txt"])
        mock_client.delete_objects.return_value = {}

        await aws_store.delete_directory("data/")

        sent = mock_client.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert {"Key": "data/"} in sent

    @pytest.mark.asyncio
    async def test_per_key_errors_are_surfaced(self, aws_store, mock_client):
        """Partial failures arrive inside a 200 response and must not be ignored."""
        from multicloud.blob.blob_client.async_blob.async_driver import BlobAsyncError

        mock_client.list_objects_v2.return_value = _list_page(
            ["data/a.txt", "data/b.txt"]
        )
        mock_client.delete_objects.return_value = {
            "Errors": [
                {
                    "Key": "data/b.txt",
                    "Code": "AccessDenied",
                    "Message": "Access Denied",
                }
            ]
        }

        with pytest.raises(BlobAsyncError) as exc_info:
            await aws_store.delete_directory("data/")

        message = str(exc_info.value)
        assert "failed to delete 1 object(s)" in message
        assert "data/b.txt" in message
        assert "AccessDenied" in message

    @pytest.mark.asyncio
    async def test_max_concurrency_is_validated(self, aws_store):
        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException, match="max_concurrency"):
            await aws_store.delete_directory("data/", max_concurrency=0)

    @pytest.mark.asyncio
    async def test_client_is_created_before_listing(self, aws_store, mock_session):
        session, client = mock_session
        client.list_objects_v2.return_value = {"IsTruncated": False}

        await aws_store.delete_directory("data/")

        session.create_client.assert_called_once()
