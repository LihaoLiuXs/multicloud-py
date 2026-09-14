"""Unit tests for the shared S3 client config helper."""

from __future__ import annotations

import builtins
import importlib

import pytest
from botocore.config import Config
from multicloud.aws_common.aws_client_config import s3_client_config


def test_pins_sigv4():
    # The AWS SDK would otherwise emit deprecated SigV2 presigned URLs in
    # every region whose endpoint metadata still advertises SigV2.
    assert s3_client_config().signature_version == "s3v4"


def test_returns_botocore_config():
    assert isinstance(s3_client_config(), Config)


def test_does_not_force_virtual_addressing():
    # Virtual addressing would break custom endpoints (MinIO/LocalStack) and
    # pin the host to the configured region. The default global endpoint
    # already accepts a correctly-scoped SigV4 signature.
    assert s3_client_config().s3 is None


def test_overrides_are_forwarded():
    config = s3_client_config(read_timeout=42)

    assert config.read_timeout == 42
    assert config.signature_version == "s3v4"


def test_signature_version_override_is_ignored_not_a_crash():
    # A caller passing signature_version must not raise "got multiple values
    # for keyword argument", and must not be able to downgrade off SigV4 --
    # the pin is authoritative.
    config = s3_client_config(signature_version="s3", read_timeout=42)

    assert config.signature_version == "s3v4"
    assert config.read_timeout == 42


def test_missing_botocore_raises_actionable_import_error(monkeypatch):
    # When botocore is not installed, importing the module must fail with a
    # clear "install the [aws] extra" message, not a bare ModuleNotFoundError.
    import multicloud.aws_common.aws_client_config as module

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "botocore.config" or name.startswith("botocore.config"):
            raise ImportError("No module named 'botocore'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match=r"python-multicloud-sdk\[aws\]"):
        importlib.reload(module)

    # Restore a healthy module for any later importers in the session.
    monkeypatch.undo()
    importlib.reload(module)
