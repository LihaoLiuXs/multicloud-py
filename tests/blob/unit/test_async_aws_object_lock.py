"""Unit tests for the AWS async get_object_lock implementation.

Tests validate AwsAsyncBlobStore.do_get_object_lock:
- retention + legal hold are two independent sub-resource reads
- each sub-resource reads "absent" from NoSuchObjectLockConfiguration only
- GOVERNANCE and COMPLIANCE map to distinct modes
- event_based_hold is None (not reported), never False
- version_id pins BOTH sub-resource reads
- an unlocked object reads as None rather than an empty lock
- other native errors propagate for client-layer translation
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    ObjectLockInfo,
    ObjectRetentionConfig,
    RetentionMode,
)
from multicloud.common.exceptions import FailedPreconditionException

try:
    from botocore.exceptions import ClientError
except ImportError:
    pytest.skip("aiobotocore not installed", allow_module_level=True)


GOVERNANCE_UNTIL = datetime(2030, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
COMPLIANCE_UNTIL = datetime(2031, 8, 9, 10, 11, 12, tzinfo=timezone.utc)
LATER_UNTIL = datetime(2032, 12, 25, 1, 2, 3, tzinfo=timezone.utc)


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObjectRetention",
    )


def _no_lock_error() -> ClientError:
    return _client_error("NoSuchObjectLockConfiguration", 404)


def _raising(exc):
    """A coroutine function that raises when awaited."""

    async def _call(**kwargs):
        raise exc

    return _call


@pytest.fixture
def mock_s3_client():
    mock = AsyncMock()
    mock.get_object_retention = AsyncMock()
    mock.get_object_legal_hold = AsyncMock()
    mock.put_object_retention = AsyncMock()
    mock.put_object_legal_hold = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def store(mock_s3_client):
    with patch(
        "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session"
    ):
        store = AwsAsyncBlobStore(bucket="lock-bucket", region="us-west-2")
        store._client = mock_s3_client
        store._client_cm = MagicMock()
        yield store
        await store.close()


@pytest.mark.asyncio
class TestAwsAsyncGetObjectLock:
    """Tests for AwsAsyncBlobStore.do_get_object_lock."""

    async def test_governance_retention_and_legal_hold(self, store, mock_s3_client):
        """Both sub-resources set are combined into one ObjectLockInfo."""
        mock_s3_client.get_object_retention.return_value = {
            "Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": GOVERNANCE_UNTIL}
        }
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "ON"}
        }

        result = await store.do_get_object_lock("locked.txt")

        assert isinstance(result, ObjectLockInfo)
        assert result.mode is RetentionMode.GOVERNANCE
        assert result.retain_until_date == GOVERNANCE_UNTIL
        assert result.legal_hold is True
        # S3 has no event-based hold; None is "not reported", not "off".
        assert result.event_based_hold is None

    async def test_compliance_mode_is_distinct_from_governance(
        self, store, mock_s3_client
    ):
        """COMPLIANCE retention maps to its own mode and its own instant."""
        mock_s3_client.get_object_retention.return_value = {
            "Retention": {"Mode": "COMPLIANCE", "RetainUntilDate": COMPLIANCE_UNTIL}
        }
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "OFF"}
        }

        result = await store.do_get_object_lock("compliance.txt")

        assert result is not None
        assert result.mode is RetentionMode.COMPLIANCE
        assert result.retain_until_date == COMPLIANCE_UNTIL
        assert result.retain_until_date != GOVERNANCE_UNTIL
        assert result.legal_hold is False

    async def test_retention_only_when_legal_hold_absent(self, store, mock_s3_client):
        """A missing legal-hold sub-resource does not hide the retention."""
        mock_s3_client.get_object_retention.return_value = {
            "Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": GOVERNANCE_UNTIL}
        }
        mock_s3_client.get_object_legal_hold.side_effect = _no_lock_error()

        result = await store.do_get_object_lock("retention-only.txt")

        assert result is not None
        assert result.mode is RetentionMode.GOVERNANCE
        assert result.retain_until_date == GOVERNANCE_UNTIL
        assert result.legal_hold is False

    async def test_legal_hold_only_when_retention_absent(self, store, mock_s3_client):
        """A missing retention sub-resource does not hide the legal hold."""
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "ON"}
        }

        result = await store.do_get_object_lock("hold-only.txt")

        assert result is not None
        assert result.mode is None
        assert result.retain_until_date is None
        assert result.legal_hold is True

    async def test_unlocked_object_returns_none(self, store, mock_s3_client):
        """Neither sub-resource configured reads as None, not an empty lock."""
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()
        mock_s3_client.get_object_legal_hold.side_effect = _no_lock_error()

        assert await store.do_get_object_lock("plain.txt") is None

    async def test_legal_hold_off_with_no_retention_returns_none(
        self, store, mock_s3_client
    ):
        """An explicitly-OFF hold and no retention is still an unlocked object."""
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "OFF"}
        }

        assert await store.do_get_object_lock("released.txt") is None

    async def test_empty_retention_payload_reads_as_absent(self, store, mock_s3_client):
        """A Retention block with no Mode does not fabricate a lock."""
        mock_s3_client.get_object_retention.return_value = {"Retention": {}}
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "OFF"}
        }

        assert await store.do_get_object_lock("empty-retention.txt") is None

    async def test_empty_legal_hold_payload_reads_as_absent(
        self, store, mock_s3_client
    ):
        """A LegalHold block with no Status does not fabricate a hold.

        Status is optional in the S3 model, so an absent one must read as "no
        hold" rather than being inferred from "not OFF".
        """
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()
        mock_s3_client.get_object_legal_hold.return_value = {"LegalHold": {}}

        assert await store.do_get_object_lock("empty-hold.txt") is None

    async def test_absent_legal_hold_status_does_not_hold_a_retained_object(
        self, store, mock_s3_client
    ):
        """A retained object with no hold Status is retained, not held."""
        mock_s3_client.get_object_retention.return_value = {
            "Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": GOVERNANCE_UNTIL}
        }
        mock_s3_client.get_object_legal_hold.return_value = {"LegalHold": {}}

        result = await store.do_get_object_lock("retained-no-hold.txt")

        assert result is not None
        assert result.mode is RetentionMode.GOVERNANCE
        assert result.legal_hold is False

    async def test_both_sub_resources_are_read(self, store, mock_s3_client):
        """Neither read is skipped, whichever completes first."""
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()
        mock_s3_client.get_object_legal_hold.side_effect = _no_lock_error()

        await store.do_get_object_lock("plain.txt")

        mock_s3_client.get_object_retention.assert_awaited_once()
        mock_s3_client.get_object_legal_hold.assert_awaited_once()

    async def test_version_id_pins_both_sub_resource_reads(self, store, mock_s3_client):
        """version_id must reach the retention AND the legal-hold call."""
        mock_s3_client.get_object_retention.return_value = {
            "Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": GOVERNANCE_UNTIL}
        }
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "ON"}
        }

        await store.do_get_object_lock("versioned.txt", version_id="v-77")

        retention_kwargs = mock_s3_client.get_object_retention.call_args.kwargs
        hold_kwargs = mock_s3_client.get_object_legal_hold.call_args.kwargs
        assert retention_kwargs == {
            "Bucket": "lock-bucket",
            "Key": "versioned.txt",
            "VersionId": "v-77",
        }
        assert hold_kwargs == {
            "Bucket": "lock-bucket",
            "Key": "versioned.txt",
            "VersionId": "v-77",
        }

    async def test_omitted_version_id_sends_no_version(self, store, mock_s3_client):
        """Without a version_id the calls must not carry a VersionId key."""
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()
        mock_s3_client.get_object_legal_hold.side_effect = _no_lock_error()

        await store.do_get_object_lock("latest.txt")

        assert "VersionId" not in mock_s3_client.get_object_retention.call_args.kwargs
        assert "VersionId" not in mock_s3_client.get_object_legal_hold.call_args.kwargs

    async def test_access_denied_on_retention_propagates(self, store, mock_s3_client):
        """Only NoSuchObjectLockConfiguration is swallowed; AccessDenied is not."""
        denied = _client_error("AccessDenied", 403)
        mock_s3_client.get_object_retention.side_effect = denied
        mock_s3_client.get_object_legal_hold.return_value = {
            "LegalHold": {"Status": "ON"}
        }

        with pytest.raises(ClientError) as exc_info:
            await store.do_get_object_lock("denied.txt")

        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"

    async def test_missing_object_propagates(self, store, mock_s3_client):
        """A missing object is a real error, not an absent sub-resource."""
        mock_s3_client.get_object_retention.side_effect = _client_error(
            "NoSuchKey", 404
        )

        with pytest.raises(ClientError) as exc_info:
            await store.do_get_object_lock("gone.txt")

        assert exc_info.value.response["Error"]["Code"] == "NoSuchKey"

    async def test_access_denied_on_legal_hold_propagates(self, store, mock_s3_client):
        """A denied legal-hold read is not downgraded to 'no hold'."""
        mock_s3_client.get_object_retention.return_value = {
            "Retention": {"Mode": "GOVERNANCE", "RetainUntilDate": GOVERNANCE_UNTIL}
        }
        mock_s3_client.get_object_legal_hold.side_effect = _client_error(
            "AccessDenied", 403
        )

        with pytest.raises(ClientError) as exc_info:
            await store.do_get_object_lock("half-denied.txt")

        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"


@pytest.mark.asyncio
class TestAwsAsyncUpdateLegalHold:
    """Tests for AwsAsyncBlobStore.do_update_legal_hold."""

    async def test_enable_sends_status_on(self, store, mock_s3_client):
        await store.do_update_legal_hold("held.txt", True)

        sent = mock_s3_client.put_object_legal_hold.call_args.kwargs
        assert sent["LegalHold"] == {"Status": "ON"}
        assert sent["Bucket"] == "lock-bucket"
        assert sent["Key"] == "held.txt"

    async def test_disable_sends_status_off(self, store, mock_s3_client):
        """Removal is an explicit OFF, not an omitted field."""
        await store.do_update_legal_hold("released.txt", False)

        sent = mock_s3_client.put_object_legal_hold.call_args.kwargs
        assert sent["LegalHold"] == {"Status": "OFF"}

    async def test_version_id_pins_the_write(self, store, mock_s3_client):
        """A hold applies to a version, so the version must reach the call."""
        await store.do_update_legal_hold("versioned.txt", True, version_id="v-88")

        assert (
            mock_s3_client.put_object_legal_hold.call_args.kwargs["VersionId"] == "v-88"
        )

    async def test_omitted_version_id_sends_no_version(self, store, mock_s3_client):
        await store.do_update_legal_hold("latest.txt", True)

        assert "VersionId" not in mock_s3_client.put_object_legal_hold.call_args.kwargs

    async def test_provider_error_propagates(self, store, mock_s3_client):
        """A bucket without object lock is an error, not a silent no-op."""
        mock_s3_client.put_object_legal_hold = _raising(
            _client_error("InvalidRequest", 400)
        )

        with pytest.raises(ClientError) as exc_info:
            await store.do_update_legal_hold("no-lock-bucket.txt", True)

        assert exc_info.value.response["Error"]["Code"] == "InvalidRequest"


@pytest.mark.asyncio
class TestAwsAsyncUpdateObjectRetention:
    """Tests for AwsAsyncBlobStore.do_update_object_retention."""

    @staticmethod
    def _current(mode: str, until):
        return {"Retention": {"Mode": mode, "RetainUntilDate": until}}

    async def test_extend_writes_the_new_instant(self, store, mock_s3_client):
        """An extension writes the requested instant, not the current one."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", GOVERNANCE_UNTIL
        )

        await store.do_update_object_retention(
            "extend.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        written = mock_s3_client.put_object_retention.call_args.kwargs
        assert written["Retention"]["RetainUntilDate"] == LATER_UNTIL
        assert written["Retention"]["RetainUntilDate"] != GOVERNANCE_UNTIL
        assert written["Bucket"] == "lock-bucket"
        assert written["Key"] == "extend.txt"

    async def test_omitted_mode_preserves_governance(self, store, mock_s3_client):
        """A None config mode keeps the mutable mode already on the object."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", GOVERNANCE_UNTIL
        )

        await store.do_update_object_retention(
            "gov.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        written = mock_s3_client.put_object_retention.call_args.kwargs
        assert written["Retention"]["Mode"] == "GOVERNANCE"

    async def test_omitted_mode_preserves_compliance(self, store, mock_s3_client):
        """The preserved mode is read from the object, not defaulted."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "COMPLIANCE", COMPLIANCE_UNTIL
        )

        await store.do_update_object_retention(
            "comp.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        written = mock_s3_client.put_object_retention.call_args.kwargs
        assert written["Retention"]["Mode"] == "COMPLIANCE"

    async def test_explicit_mode_upgrade_with_bypass_is_written(
        self, store, mock_s3_client
    ):
        """An explicit mode replaces the current one when the rules allow it."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", GOVERNANCE_UNTIL
        )

        await store.do_update_object_retention(
            "upgrade.txt",
            ObjectRetentionConfig(
                retain_until_date=LATER_UNTIL,
                mode=RetentionMode.COMPLIANCE,
                bypass_governance=True,
            ),
        )

        written = mock_s3_client.put_object_retention.call_args.kwargs
        assert written["Retention"]["Mode"] == "COMPLIANCE"
        assert written["BypassGovernanceRetention"] is True

    async def test_bypass_flag_is_omitted_when_not_requested(
        self, store, mock_s3_client
    ):
        """The bypass flag is a deliberate escalation, never sent by default."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", GOVERNANCE_UNTIL
        )

        await store.do_update_object_retention(
            "plain.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        assert (
            "BypassGovernanceRetention"
            not in mock_s3_client.put_object_retention.call_args.kwargs
        )

    async def test_version_id_pins_the_read_and_the_write(self, store, mock_s3_client):
        """A versioned update must not read one version and write another."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", GOVERNANCE_UNTIL
        )

        await store.do_update_object_retention(
            "versioned.txt",
            ObjectRetentionConfig(retain_until_date=LATER_UNTIL),
            version_id="v-55",
        )

        assert (
            mock_s3_client.get_object_retention.call_args.kwargs["VersionId"] == "v-55"
        )
        assert (
            mock_s3_client.put_object_retention.call_args.kwargs["VersionId"] == "v-55"
        )

    async def test_object_without_retention_is_refused_before_writing(
        self, store, mock_s3_client
    ):
        """Update does not establish retention, so it refuses without writing."""
        mock_s3_client.get_object_retention.side_effect = _no_lock_error()

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "unretained.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
            )

        mock_s3_client.put_object_retention.assert_not_awaited()

    async def test_shorten_without_bypass_is_refused_before_writing(
        self, store, mock_s3_client
    ):
        """A disallowed shorten must not reach the provider at all."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", LATER_UNTIL
        )

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "shorten.txt",
                ObjectRetentionConfig(retain_until_date=GOVERNANCE_UNTIL),
            )

        mock_s3_client.put_object_retention.assert_not_awaited()

    async def test_shorten_with_bypass_is_written(self, store, mock_s3_client):
        """With bypass the shorter instant is the one written."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "GOVERNANCE", LATER_UNTIL
        )

        await store.do_update_object_retention(
            "shorten.txt",
            ObjectRetentionConfig(
                retain_until_date=GOVERNANCE_UNTIL, bypass_governance=True
            ),
        )

        written = mock_s3_client.put_object_retention.call_args.kwargs
        assert written["Retention"]["RetainUntilDate"] == GOVERNANCE_UNTIL
        assert written["BypassGovernanceRetention"] is True

    async def test_compliance_shorten_is_refused_even_with_bypass(
        self, store, mock_s3_client
    ):
        """Bypass does not unlock the immutable mode."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "COMPLIANCE", LATER_UNTIL
        )

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "immutable.txt",
                ObjectRetentionConfig(
                    retain_until_date=GOVERNANCE_UNTIL, bypass_governance=True
                ),
            )

        mock_s3_client.put_object_retention.assert_not_awaited()

    async def test_mode_downgrade_is_refused(self, store, mock_s3_client):
        """COMPLIANCE cannot be relaxed to GOVERNANCE."""
        mock_s3_client.get_object_retention.return_value = self._current(
            "COMPLIANCE", GOVERNANCE_UNTIL
        )

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "downgrade.txt",
                ObjectRetentionConfig(
                    retain_until_date=LATER_UNTIL,
                    mode=RetentionMode.GOVERNANCE,
                    bypass_governance=True,
                ),
            )

        mock_s3_client.put_object_retention.assert_not_awaited()

    async def test_read_failure_propagates_without_writing(self, store, mock_s3_client):
        """A denied read is not treated as 'no current retention'."""
        mock_s3_client.get_object_retention.side_effect = _client_error(
            "AccessDenied", 403
        )

        with pytest.raises(ClientError) as exc_info:
            await store.do_update_object_retention(
                "denied.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
            )

        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
        mock_s3_client.put_object_retention.assert_not_awaited()
