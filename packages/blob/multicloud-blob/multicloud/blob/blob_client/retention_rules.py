"""State-dependent retention rules shared by every provider's
``do_update_object_retention`` hook.

Each provider reads the object's current retention, then calls
:func:`resolve_and_validate` to compute the effective mode to write back (or to
reject the request). Centralizing the rules here guarantees every provider
surfaces the same exception types and messages for the same situation.

Stateless validation (null config, null retain-until-date) lives in
:class:`~multicloud.blob.blob_client.validators.BlobStoreValidator` and runs in
the driver template before the provider hook. By the time control reaches this
helper, both ``config`` and ``config.retain_until_date`` are guaranteed
non-null.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from multicloud.common.exceptions import FailedPreconditionException

if TYPE_CHECKING:
    from multicloud.blob.blob_client.driver import (
        ObjectRetentionConfig,
        RetentionMode,
    )

NO_CURRENT_RETENTION_MSG = (
    "Object does not have retention configured. Cannot update retention."
)
MODE_DOWNGRADE_MSG = (
    "Cannot downgrade retention mode from COMPLIANCE to GOVERNANCE. "
    "The immutable mode does not permit downgrade on AWS or GCP."
)
MODE_UPGRADE_NO_BYPASS_MSG = (
    "Cannot upgrade retention mode from GOVERNANCE to COMPLIANCE without "
    "bypass_governance=True. Both AWS S3 and GCP GCS treat the lock-mode "
    "transition as a modification of the existing lock and require the "
    "bypass/override flag."
)
COMPLIANCE_SHORTEN_MSG = (
    "Cannot shorten retention for objects in COMPLIANCE mode. "
    "bypass_governance has no effect on the immutable mode."
)
GOVERNANCE_SHORTEN_NO_BYPASS_MSG = (
    "Cannot shorten retention for objects in GOVERNANCE mode without "
    "bypass_governance=True."
)


def resolve_and_validate(
    current_mode: Optional["RetentionMode"],
    current_retain_until: Optional[datetime],
    config: "ObjectRetentionConfig",
) -> "RetentionMode":
    """Resolve the effective new retention against the rules table.

    Args:
        current_mode: The object's current retention mode, or None if it has
            no retention configured.
        current_retain_until: The object's current retain-until instant, or
            None if it has no retention configured.
        config: The requested change. ``config.retain_until_date`` is
            guaranteed non-null by the stateless validator.

    Returns:
        The retention mode to write back: ``config.mode`` when it is set,
        otherwise the object's ``current_mode`` (preserve).

    Raises:
        FailedPreconditionException: When a rule rejects the request — the
            object has no current retention, an immutable-mode downgrade,
            a mode upgrade without bypass, or a disallowed shorten.
    """
    from multicloud.blob.blob_client.driver import RetentionMode

    # 1. No current retention -> reject. Update targets objects that already
    #    have retention; first-time set belongs on the upload path.
    if current_mode is None:
        raise FailedPreconditionException(NO_CURRENT_RETENTION_MSG)

    effective_mode = config.mode if config.mode is not None else current_mode
    bypass = config.bypass_governance

    # 2. Mode downgrade COMPLIANCE -> GOVERNANCE is rejected by both clouds.
    if (
        current_mode == RetentionMode.COMPLIANCE
        and effective_mode == RetentionMode.GOVERNANCE
    ):
        raise FailedPreconditionException(MODE_DOWNGRADE_MSG)

    # 2b. Mode upgrade GOVERNANCE -> COMPLIANCE requires bypass_governance=True.
    #     Both clouds treat the mode change as a modification of the existing
    #     lock and reject it server-side without the bypass/override flag.
    if (
        current_mode == RetentionMode.GOVERNANCE
        and effective_mode == RetentionMode.COMPLIANCE
        and not bypass
    ):
        raise FailedPreconditionException(MODE_UPGRADE_NO_BYPASS_MSG)

    # 3. Shorten check — strict less-than; equal dates are not "shortening".
    shortening = (
        current_retain_until is not None
        and config.retain_until_date < current_retain_until
    )
    if shortening:
        # 3a. COMPLIANCE is extend-only regardless of bypass — both clouds
        #     ignore the flag on the immutable mode.
        if current_mode == RetentionMode.COMPLIANCE:
            raise FailedPreconditionException(COMPLIANCE_SHORTEN_MSG)
        # 3b. GOVERNANCE requires explicit bypass to shorten.
        if not bypass:
            raise FailedPreconditionException(GOVERNANCE_SHORTEN_NO_BYPASS_MSG)

    return effective_mode
