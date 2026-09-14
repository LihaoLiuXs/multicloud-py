"""Unit tests for the shared retention rules table (resolve_and_validate)."""

from datetime import datetime, timezone

import pytest
from multicloud.blob.blob_client.driver import ObjectRetentionConfig, RetentionMode
from multicloud.blob.blob_client.retention_rules import (
    COMPLIANCE_SHORTEN_MSG,
    GOVERNANCE_SHORTEN_NO_BYPASS_MSG,
    MODE_DOWNGRADE_MSG,
    MODE_UPGRADE_NO_BYPASS_MSG,
    NO_CURRENT_RETENTION_MSG,
    resolve_and_validate,
)
from multicloud.common.exceptions import FailedPreconditionException

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
LATER = datetime(2031, 1, 1, tzinfo=timezone.utc)
EARLIER = datetime(2029, 1, 1, tzinfo=timezone.utc)


def _cfg(mode, retain_until, *, bypass=False):
    return ObjectRetentionConfig(
        retain_until_date=retain_until, mode=mode, bypass_governance=bypass
    )


class TestNoCurrentRetention:
    def test_none_current_mode_rejected(self):
        with pytest.raises(FailedPreconditionException, match=NO_CURRENT_RETENTION_MSG):
            resolve_and_validate(None, None, _cfg(RetentionMode.GOVERNANCE, LATER))


class TestModeTransitions:
    def test_downgrade_compliance_to_governance_rejected(self):
        with pytest.raises(FailedPreconditionException, match=MODE_DOWNGRADE_MSG):
            resolve_and_validate(
                RetentionMode.COMPLIANCE, NOW, _cfg(RetentionMode.GOVERNANCE, LATER)
            )

    def test_upgrade_governance_to_compliance_without_bypass_rejected(self):
        with pytest.raises(
            FailedPreconditionException, match=MODE_UPGRADE_NO_BYPASS_MSG
        ):
            resolve_and_validate(
                RetentionMode.GOVERNANCE, NOW, _cfg(RetentionMode.COMPLIANCE, LATER)
            )

    def test_upgrade_governance_to_compliance_with_bypass_allowed(self):
        resolved = resolve_and_validate(
            RetentionMode.GOVERNANCE,
            NOW,
            _cfg(RetentionMode.COMPLIANCE, LATER, bypass=True),
        )
        assert resolved is RetentionMode.COMPLIANCE

    def test_none_mode_preserves_current(self):
        resolved = resolve_and_validate(
            RetentionMode.GOVERNANCE, NOW, _cfg(None, LATER)
        )
        assert resolved is RetentionMode.GOVERNANCE


class TestShortening:
    def test_shorten_compliance_rejected_even_with_bypass(self):
        with pytest.raises(FailedPreconditionException, match=COMPLIANCE_SHORTEN_MSG):
            resolve_and_validate(
                RetentionMode.COMPLIANCE,
                NOW,
                _cfg(RetentionMode.COMPLIANCE, EARLIER, bypass=True),
            )

    def test_shorten_governance_without_bypass_rejected(self):
        with pytest.raises(
            FailedPreconditionException, match=GOVERNANCE_SHORTEN_NO_BYPASS_MSG
        ):
            resolve_and_validate(
                RetentionMode.GOVERNANCE, NOW, _cfg(RetentionMode.GOVERNANCE, EARLIER)
            )

    def test_shorten_governance_with_bypass_allowed(self):
        resolved = resolve_and_validate(
            RetentionMode.GOVERNANCE,
            NOW,
            _cfg(RetentionMode.GOVERNANCE, EARLIER, bypass=True),
        )
        assert resolved is RetentionMode.GOVERNANCE

    def test_extend_allowed_without_bypass(self):
        resolved = resolve_and_validate(
            RetentionMode.GOVERNANCE, NOW, _cfg(RetentionMode.GOVERNANCE, LATER)
        )
        assert resolved is RetentionMode.GOVERNANCE

    def test_equal_date_is_not_shortening(self):
        # Equal retain-until is not "shortening"; allowed without bypass.
        resolved = resolve_and_validate(
            RetentionMode.GOVERNANCE, NOW, _cfg(RetentionMode.GOVERNANCE, NOW)
        )
        assert resolved is RetentionMode.GOVERNANCE
