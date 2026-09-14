"""Unit tests for the GCP async get_object_lock implementation.

Tests validate GcpAsyncBlobStore.do_get_object_lock:
- Locked and Unlocked retention map to distinct modes
- retainUntilTime is parsed into a timezone-aware datetime
- either hold flag reports a legal hold; event_based_hold is reported as a bool
- an omitted hold flag reads as False, not None
- version_id pins the read to a generation
- an unlocked object reads as None rather than an empty lock
- a missing object raises BlobNotFoundError
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from multicloud.blob.blob_client.async_blob.async_driver import (
    BlobNotFoundError,
    ObjectLockInfo,
    ObjectRetentionConfig,
    RetentionMode,
)
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceNotFoundException,
)

try:
    import aiohttp
except ImportError:
    pytest.skip("aiohttp not installed", allow_module_level=True)


UNLOCKED_UNTIL_RAW = "2030-03-04T05:06:07Z"
UNLOCKED_UNTIL = datetime(2030, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
LOCKED_UNTIL_RAW = "2031-08-09T10:11:12Z"
LOCKED_UNTIL = datetime(2031, 8, 9, 10, 11, 12, tzinfo=timezone.utc)
LATER_UNTIL = datetime(2032, 12, 25, 1, 2, 3, tzinfo=timezone.utc)
LATER_UNTIL_RAW = "2032-12-25T01:02:03.000Z"
UNLOCKED_UNTIL_RAW_MS = "2030-03-04T05:06:07.000Z"


@pytest.fixture
def mock_storage():
    mock = AsyncMock()
    mock.download_metadata = AsyncMock()
    mock._download = AsyncMock()
    mock.patch_metadata = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def store(mock_storage):
    store = GcpAsyncBlobStore(
        bucket="lock-bucket", region="us-central1", _storage=mock_storage
    )
    yield store
    await store.close()


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status, message="error"
    )


@pytest.mark.asyncio
class TestGcpAsyncGetObjectLock:
    """Tests for GcpAsyncBlobStore.do_get_object_lock."""

    async def test_unlocked_retention_maps_to_governance(self, store, mock_storage):
        """An Unlocked retention is the mutable mode."""
        mock_storage.download_metadata.return_value = {
            "name": "gov.txt",
            "retention": {"mode": "Unlocked", "retainUntilTime": UNLOCKED_UNTIL_RAW},
        }

        result = await store.do_get_object_lock("gov.txt")

        assert isinstance(result, ObjectLockInfo)
        assert result.mode is RetentionMode.GOVERNANCE
        assert result.retain_until_date == UNLOCKED_UNTIL
        assert result.legal_hold is False
        # The flag is absent from the resource, which means off, not unreported.
        assert result.event_based_hold is False

    async def test_locked_retention_maps_to_compliance(self, store, mock_storage):
        """A Locked retention is the immutable mode, with its own instant."""
        mock_storage.download_metadata.return_value = {
            "name": "locked.txt",
            "retention": {"mode": "Locked", "retainUntilTime": LOCKED_UNTIL_RAW},
        }

        result = await store.do_get_object_lock("locked.txt")

        assert result is not None
        assert result.mode is RetentionMode.COMPLIANCE
        assert result.retain_until_date == LOCKED_UNTIL
        assert result.retain_until_date != UNLOCKED_UNTIL

    async def test_parses_the_sub_second_format_the_provider_actually_sends(
        self, store, mock_storage
    ):
        """The recorded wire format carries six fractional digits.

        Taken from a real recorded response rather than invented, so a parser
        that only handled whole seconds would fail here instead of in production.
        """
        mock_storage.download_metadata.return_value = {
            "retention": {
                "mode": "Locked",
                "retainUntilTime": "2026-09-30T00:00:00.000000Z",
            }
        }

        result = await store.do_get_object_lock("micros.txt")

        assert result is not None
        assert result.retain_until_date == datetime(
            2026, 9, 30, 0, 0, 0, tzinfo=timezone.utc
        )

    async def test_retention_with_null_retain_until_is_still_a_lock(
        self, store, mock_storage
    ):
        """A mode with an explicitly null instant is retention with no expiry set.

        Recorded responses do carry ``retainUntilTime: null``, so this must not
        crash and must not be downgraded to "unlocked".
        """
        mock_storage.download_metadata.return_value = {
            "retention": {"mode": "Locked", "retainUntilTime": None}
        }

        result = await store.do_get_object_lock("null-until.txt")

        assert result is not None
        assert result.mode is RetentionMode.COMPLIANCE
        assert result.retain_until_date is None

    async def test_retain_until_is_timezone_aware(self, store, mock_storage):
        """The parsed instant carries a UTC offset, not a naive wall clock."""
        mock_storage.download_metadata.return_value = {
            "retention": {"mode": "Unlocked", "retainUntilTime": UNLOCKED_UNTIL_RAW}
        }

        result = await store.do_get_object_lock("tz.txt")

        assert result is not None
        assert result.retain_until_date is not None
        assert result.retain_until_date.tzinfo is not None
        assert result.retain_until_date.utcoffset() == timezone.utc.utcoffset(None)

    async def test_temporary_hold_reports_legal_hold_without_event_hold(
        self, store, mock_storage
    ):
        """A temporary hold holds the object but is not an event-based hold."""
        mock_storage.download_metadata.return_value = {"temporaryHold": True}

        result = await store.do_get_object_lock("temp-hold.txt")

        assert result is not None
        assert result.legal_hold is True
        assert result.event_based_hold is False
        assert result.mode is None
        assert result.retain_until_date is None

    async def test_event_based_hold_reports_both_flags(self, store, mock_storage):
        """An event-based hold is reported as a legal hold and in its own field."""
        mock_storage.download_metadata.return_value = {"eventBasedHold": True}

        result = await store.do_get_object_lock("event-hold.txt")

        assert result is not None
        assert result.legal_hold is True
        assert result.event_based_hold is True

    async def test_explicitly_false_holds_read_as_unlocked(self, store, mock_storage):
        """Holds present but off, with no retention, is an unlocked object."""
        mock_storage.download_metadata.return_value = {
            "temporaryHold": False,
            "eventBasedHold": False,
        }

        assert await store.do_get_object_lock("released.txt") is None

    async def test_object_with_no_lock_fields_returns_none(self, store, mock_storage):
        """A plain object reads as None rather than an empty lock."""
        mock_storage.download_metadata.return_value = {
            "name": "plain.txt",
            "size": "5",
            "etag": "abc",
        }

        assert await store.do_get_object_lock("plain.txt") is None

    async def test_retention_and_hold_combine(self, store, mock_storage):
        """Retention and a hold are reported together, not one or the other."""
        mock_storage.download_metadata.return_value = {
            "retention": {"mode": "Locked", "retainUntilTime": LOCKED_UNTIL_RAW},
            "eventBasedHold": True,
        }

        result = await store.do_get_object_lock("both.txt")

        assert result is not None
        assert result.mode is RetentionMode.COMPLIANCE
        assert result.retain_until_date == LOCKED_UNTIL
        assert result.legal_hold is True
        assert result.event_based_hold is True

    async def test_retention_without_mode_still_counts_as_retention(
        self, store, mock_storage
    ):
        """A retain-until with no mode is retention, defaulting to the mutable mode."""
        mock_storage.download_metadata.return_value = {
            "retention": {"retainUntilTime": UNLOCKED_UNTIL_RAW}
        }

        result = await store.do_get_object_lock("no-mode.txt")

        assert result is not None
        assert result.mode is RetentionMode.GOVERNANCE
        assert result.retain_until_date == UNLOCKED_UNTIL

    async def test_version_id_pins_the_generation(self, store, mock_storage):
        """A versioned read goes through the generation-pinned JSON fetch."""
        mock_storage._download.return_value = json.dumps(
            {"temporaryHold": True, "generation": "1234567890"}
        ).encode()

        result = await store.do_get_object_lock(
            "versioned.txt", version_id="1234567890"
        )

        assert result is not None
        assert result.legal_hold is True
        mock_storage.download_metadata.assert_not_awaited()
        call = mock_storage._download.call_args
        assert call.args == ("lock-bucket", "versioned.txt")
        assert call.kwargs["params"] == {"alt": "json", "generation": "1234567890"}

    async def test_non_integer_version_id_is_rejected(self, store, mock_storage):
        """A non-generation version_id fails before any request is made."""
        with pytest.raises(InvalidArgumentException):
            await store.do_get_object_lock("versioned.txt", version_id="not-a-number")

        mock_storage._download.assert_not_awaited()
        mock_storage.download_metadata.assert_not_awaited()

    async def test_missing_object_raises_blob_not_found(self, store, mock_storage):
        """A 404 becomes BlobNotFoundError carrying the key."""
        mock_storage.download_metadata.side_effect = _client_response_error(404)

        with pytest.raises(BlobNotFoundError) as exc_info:
            await store.do_get_object_lock("gone.txt")

        assert exc_info.value.key == "gone.txt"

    async def test_missing_object_is_catchable_as_resource_not_found(
        self, store, mock_storage
    ):
        """The missing-object error matches the cross-provider contract.

        A caller handling ResourceNotFoundException must not have to know
        which provider raised.
        """
        mock_storage.download_metadata.side_effect = _client_response_error(404)

        with pytest.raises(ResourceNotFoundException):
            await store.do_get_object_lock("gone.txt")

    async def test_present_but_empty_retention_is_not_a_lock(self, store, mock_storage):
        """An empty retention object must not be read as retention.

        Presence alone says nothing; only a mode or a retain-until does.
        """
        mock_storage.download_metadata.return_value = {
            "name": "empty-retention.txt",
            "retention": {},
        }

        assert await store.do_get_object_lock("empty-retention.txt") is None

    async def test_null_retention_is_not_a_lock(self, store, mock_storage):
        """An explicit JSON null for retention must not crash or fabricate a lock."""
        mock_storage.download_metadata.return_value = {
            "name": "null-retention.txt",
            "retention": None,
        }

        assert await store.do_get_object_lock("null-retention.txt") is None

    async def test_permission_error_propagates_raw(self, store, mock_storage):
        """A 403 is not swallowed into 'no lock'; it reaches the client layer."""
        mock_storage.download_metadata.side_effect = _client_response_error(403)

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await store.do_get_object_lock("denied.txt")

        assert exc_info.value.status == 403


@pytest.mark.asyncio
class TestGcpAsyncUpdateLegalHold:
    """Tests for GcpAsyncBlobStore.do_update_legal_hold."""

    @staticmethod
    def _body(mock_storage) -> dict:
        return mock_storage.patch_metadata.call_args.args[2]

    async def test_enable_sets_the_temporary_hold(self, store, mock_storage):
        """With no hold active, the temporary hold is the one placed."""
        mock_storage.download_metadata.return_value = {"metageneration": "3"}

        await store.do_update_legal_hold("held.txt", True)

        assert self._body(mock_storage) == {"temporaryHold": True}

    async def test_enable_reuses_an_active_event_based_hold(self, store, mock_storage):
        """One flag already holds the object; a second would be redundant."""
        mock_storage.download_metadata.return_value = {
            "metageneration": "3",
            "eventBasedHold": True,
        }

        await store.do_update_legal_hold("preheld.txt", True)

        body = self._body(mock_storage)
        assert body == {"eventBasedHold": True}
        assert "temporaryHold" not in body

    async def test_disable_clears_both_flags(self, store, mock_storage):
        """Leaving either flag set would keep the object held while reporting success."""
        mock_storage.download_metadata.return_value = {
            "metageneration": "4",
            "eventBasedHold": True,
            "temporaryHold": True,
        }

        await store.do_update_legal_hold("released.txt", False)

        assert self._body(mock_storage) == {
            "eventBasedHold": False,
            "temporaryHold": False,
        }

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_write_is_pinned_to_the_exact_object_just_read(
        self, store, mock_storage, enabled
    ):
        """Both directions pin, and both halves of the pin are sent.

        Releasing needs the guard as much as placing does: a release that
        lands on a replacement leaves the object the caller meant to unhold
        still held. The metageneration alone cannot tell the two apart, since
        it restarts at 1 for each new generation.
        """
        mock_storage.download_metadata.return_value = {
            "metageneration": "17",
            "generation": "1787000000000002",
            "temporaryHold": True,
        }

        await store.do_update_legal_hold("guarded.txt", enabled)

        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert params["ifMetagenerationMatch"] == "17"
        assert params["ifGenerationMatch"] == "1787000000000002"

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_version_pin_applies_to_the_read_and_the_write(
        self, store, mock_storage, enabled
    ):
        """Both halves must target the requested version, not the latest.

        Asserting only the patch would miss a read that looked at the live
        object, which is what decides the flag to write and supplies the
        preconditions -- so an unpinned read makes both derive from the wrong
        version while the patch still lands on the right one.
        """
        mock_storage._download.return_value = json.dumps(
            {"metageneration": "2", "generation": "1234567890", "temporaryHold": True}
        ).encode()

        await store.do_update_legal_hold(
            "versioned.txt", enabled, version_id="1234567890"
        )

        read_params = mock_storage._download.call_args.kwargs["params"]
        assert read_params["generation"] == "1234567890"
        mock_storage.download_metadata.assert_not_awaited()

        write_params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert write_params["generation"] == "1234567890"

    async def test_enable_on_an_already_temporarily_held_object(
        self, store, mock_storage
    ):
        """Re-placing a hold keeps the flag that is already holding it."""
        mock_storage.download_metadata.return_value = {
            "metageneration": "2",
            "generation": "1",
            "temporaryHold": True,
        }

        await store.do_update_legal_hold("already-held.txt", True)

        assert self._body(mock_storage) == {"temporaryHold": True}

    async def test_enable_when_both_flags_are_set(self, store, mock_storage):
        """With both active the event-based one is kept, and a body is still sent."""
        mock_storage.download_metadata.return_value = {
            "metageneration": "2",
            "generation": "1",
            "temporaryHold": True,
            "eventBasedHold": True,
        }

        await store.do_update_legal_hold("doubly-held.txt", True)

        assert self._body(mock_storage) == {"eventBasedHold": True}

    async def test_release_on_an_unheld_object_still_clears_both(
        self, store, mock_storage
    ):
        """Release is unconditional, so it cannot be skipped as a no-op."""
        mock_storage.download_metadata.return_value = {
            "metageneration": "2",
            "generation": "1",
        }

        await store.do_update_legal_hold("not-held.txt", False)

        assert self._body(mock_storage) == {
            "eventBasedHold": False,
            "temporaryHold": False,
        }

    async def test_missing_object_is_refused_before_writing(self, store, mock_storage):
        """A 404 on the read must not be followed by a patch."""
        mock_storage.download_metadata.side_effect = _client_response_error(404)

        with pytest.raises(BlobNotFoundError):
            await store.do_update_legal_hold("gone.txt", True)

        mock_storage.patch_metadata.assert_not_awaited()

    async def test_concurrent_modification_surfaces_as_the_provider_error(
        self, store, mock_storage
    ):
        """A failed precondition is reported, not retried or swallowed."""
        mock_storage.download_metadata.return_value = {"metageneration": "5"}
        mock_storage.patch_metadata.side_effect = _client_response_error(412)

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await store.do_update_legal_hold("raced.txt", True)

        assert exc_info.value.status == 412

    async def test_absent_guard_fields_warn_rather_than_pinning(
        self, store, mock_storage, caplog
    ):
        """Losing either half of the guard is surfaced, not silently accepted."""
        mock_storage.download_metadata.return_value = {"name": "unguarded.txt"}

        with caplog.at_level(logging.WARNING):
            await store.do_update_legal_hold("unguarded.txt", True)

        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert "ifMetagenerationMatch" not in params
        assert "ifGenerationMatch" not in params
        assert "no metageneration" in caplog.text
        assert "no generation" in caplog.text


@pytest.mark.asyncio
class TestGcpAsyncUpdateObjectRetention:
    """Tests for GcpAsyncBlobStore.do_update_object_retention."""

    @staticmethod
    def _current(mode: str, raw_until: str):
        return {"retention": {"mode": mode, "retainUntilTime": raw_until}}

    @staticmethod
    def _written(mock_storage) -> dict:
        return mock_storage.patch_metadata.call_args.args[2]["retention"]

    async def test_extend_writes_the_new_instant(self, store, mock_storage):
        """An extension patches the requested instant, not the current one."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )

        await store.do_update_object_retention(
            "extend.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        written = self._written(mock_storage)
        assert written["retainUntilTime"] == LATER_UNTIL_RAW
        assert written["retainUntilTime"] != UNLOCKED_UNTIL_RAW
        call = mock_storage.patch_metadata.call_args
        assert call.args[0] == "lock-bucket"
        assert call.args[1] == "extend.txt"

    async def test_omitted_mode_preserves_the_mutable_mode(self, store, mock_storage):
        """A None config mode keeps Unlocked rather than defaulting."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )

        await store.do_update_object_retention(
            "gov.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        assert self._written(mock_storage)["mode"] == "Unlocked"

    async def test_omitted_mode_preserves_the_immutable_mode(self, store, mock_storage):
        """The preserved mode is read off the object, not assumed."""
        mock_storage.download_metadata.return_value = self._current(
            "Locked", LOCKED_UNTIL_RAW
        )

        await store.do_update_object_retention(
            "comp.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        assert self._written(mock_storage)["mode"] == "Locked"

    async def test_mode_upgrade_with_bypass_seals_the_retention(
        self, store, mock_storage
    ):
        """An explicit immutable mode is written as the provider's name for it."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )

        await store.do_update_object_retention(
            "seal.txt",
            ObjectRetentionConfig(
                retain_until_date=LATER_UNTIL,
                mode=RetentionMode.COMPLIANCE,
                bypass_governance=True,
            ),
        )

        assert self._written(mock_storage)["mode"] == "Locked"
        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert params["overrideUnlockedRetention"] == "true"

    async def test_override_is_omitted_when_not_requested(self, store, mock_storage):
        """The override is an escalation and is never sent by default."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )

        await store.do_update_object_retention(
            "plain.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert "overrideUnlockedRetention" not in params

    async def test_version_id_pins_the_read_and_the_write(self, store, mock_storage):
        """A versioned update must not read one generation and write another."""
        mock_storage._download.return_value = json.dumps(
            self._current("Unlocked", UNLOCKED_UNTIL_RAW)
        ).encode()

        await store.do_update_object_retention(
            "versioned.txt",
            ObjectRetentionConfig(retain_until_date=LATER_UNTIL),
            version_id="1234567890",
        )

        read_params = mock_storage._download.call_args.kwargs["params"]
        assert read_params["generation"] == "1234567890"
        write_params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert write_params["generation"] == "1234567890"

    async def test_retain_until_is_sent_with_millisecond_precision(
        self, store, mock_storage
    ):
        """Sub-second precision is preserved to the millisecond the API keeps."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )
        precise = datetime(2033, 4, 5, 6, 7, 8, 912345, tzinfo=timezone.utc)

        await store.do_update_object_retention(
            "precise.txt", ObjectRetentionConfig(retain_until_date=precise)
        )

        assert (
            self._written(mock_storage)["retainUntilTime"] == "2033-04-05T06:07:08.912Z"
        )

    async def test_non_utc_instant_is_normalized_to_utc(self, store, mock_storage):
        """An offset-aware instant is sent as the same moment in UTC."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )
        offset = datetime(2033, 4, 5, 1, 7, 8, tzinfo=timezone(timedelta(hours=-5)))

        await store.do_update_object_retention(
            "offset.txt", ObjectRetentionConfig(retain_until_date=offset)
        )

        assert (
            self._written(mock_storage)["retainUntilTime"] == "2033-04-05T06:07:08.000Z"
        )

    async def test_object_without_retention_is_refused_before_writing(
        self, store, mock_storage
    ):
        """Update does not establish retention, so it refuses without patching."""
        mock_storage.download_metadata.return_value = {"name": "unretained.txt"}

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "unretained.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
            )

        mock_storage.patch_metadata.assert_not_awaited()

    async def test_shorten_without_bypass_is_refused_before_writing(
        self, store, mock_storage
    ):
        """A disallowed shorten must not reach the provider at all."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", LATER_UNTIL_RAW
        )

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "shorten.txt",
                ObjectRetentionConfig(retain_until_date=UNLOCKED_UNTIL),
            )

        mock_storage.patch_metadata.assert_not_awaited()

    async def test_shorten_with_bypass_is_written(self, store, mock_storage):
        """With the override the shorter instant is the one patched."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", LATER_UNTIL_RAW
        )

        await store.do_update_object_retention(
            "shorten.txt",
            ObjectRetentionConfig(
                retain_until_date=UNLOCKED_UNTIL, bypass_governance=True
            ),
        )

        assert self._written(mock_storage)["retainUntilTime"] == UNLOCKED_UNTIL_RAW_MS
        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert params["overrideUnlockedRetention"] == "true"

    async def test_immutable_shorten_is_refused_even_with_bypass(
        self, store, mock_storage
    ):
        """The override does not unlock a sealed retention."""
        mock_storage.download_metadata.return_value = self._current(
            "Locked", LATER_UNTIL_RAW
        )

        with pytest.raises(FailedPreconditionException):
            await store.do_update_object_retention(
                "immutable.txt",
                ObjectRetentionConfig(
                    retain_until_date=UNLOCKED_UNTIL, bypass_governance=True
                ),
            )

        mock_storage.patch_metadata.assert_not_awaited()

    async def test_missing_object_is_refused_before_writing(self, store, mock_storage):
        """A 404 on the read is not treated as 'no current retention'."""
        mock_storage.download_metadata.side_effect = _client_response_error(404)

        with pytest.raises(BlobNotFoundError):
            await store.do_update_object_retention(
                "gone.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
            )

        mock_storage.patch_metadata.assert_not_awaited()

    async def test_write_is_pinned_to_the_exact_object_just_read(
        self, store, mock_storage
    ):
        """The rules were resolved against that read, so the write must pin it.

        Both halves are asserted. The metageneration restarts at 1 for each new
        generation, so on its own it does not distinguish the object read from
        a replacement written a moment later.
        """
        current = self._current("Unlocked", UNLOCKED_UNTIL_RAW)
        current["metageneration"] = "23"
        current["generation"] = "1787000000000001"
        mock_storage.download_metadata.return_value = current

        await store.do_update_object_retention(
            "guarded.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
        )

        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert params["ifMetagenerationMatch"] == "23"
        assert params["ifGenerationMatch"] == "1787000000000001"

    async def test_absent_generation_warns_rather_than_pinning(
        self, store, mock_storage, caplog
    ):
        """Losing the generation half of the guard is surfaced too."""
        current = self._current("Unlocked", UNLOCKED_UNTIL_RAW)
        current["metageneration"] = "4"
        mock_storage.download_metadata.return_value = current

        with caplog.at_level(logging.WARNING):
            await store.do_update_object_retention(
                "half-guarded.txt",
                ObjectRetentionConfig(retain_until_date=LATER_UNTIL),
            )

        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert "ifGenerationMatch" not in params
        assert params["ifMetagenerationMatch"] == "4"
        assert "no generation" in caplog.text

    async def test_concurrent_modification_surfaces_as_the_provider_error(
        self, store, mock_storage
    ):
        """A failed precondition is reported rather than retried or ignored."""
        current = self._current("Unlocked", UNLOCKED_UNTIL_RAW)
        current["metageneration"] = "5"
        mock_storage.download_metadata.return_value = current
        mock_storage.patch_metadata.side_effect = _client_response_error(412)

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await store.do_update_object_retention(
                "raced.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
            )

        assert exc_info.value.status == 412

    async def test_absent_metageneration_warns_rather_than_pinning(
        self, store, mock_storage, caplog
    ):
        """Losing the guard is surfaced, not silently accepted."""
        mock_storage.download_metadata.return_value = self._current(
            "Unlocked", UNLOCKED_UNTIL_RAW
        )

        with caplog.at_level(logging.WARNING):
            await store.do_update_object_retention(
                "unguarded.txt", ObjectRetentionConfig(retain_until_date=LATER_UNTIL)
            )

        params = mock_storage.patch_metadata.call_args.kwargs["params"]
        assert "ifMetagenerationMatch" not in params
        assert "no metageneration" in caplog.text
        assert "last-write-wins" in caplog.text
