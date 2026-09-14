# Three-Layer Architecture

Every service in the Python Multi-Cloud SDK follows a strict three-layer architecture that separates concerns between user-facing API, abstract contracts, and cloud-specific implementations.

## Overview

```
User code
  │
  ▼
┌─────────────────────────────────────┐
│  Layer 1 — Client                   │  BucketClient, StsClient
│  (Public API, cloud-agnostic)       │
└───────────────┬─────────────────────┘
                │ delegates to
                ▼
┌─────────────────────────────────────┐
│  Layer 2 — Driver                   │  BaseBlobStore, BaseSts
│  (Abstract contract + validation)   │
└───────────────┬─────────────────────┘
                │ calls do_* hooks
                ▼
┌─────────────────────────────────────┐
│  Layer 3 — Provider                 │  AwsBlobStore, GcpBlobStore, AwsSts
│  (Cloud-specific implementation)    │
└─────────────────────────────────────┘
```

## Layer Details

### Layer 1 — Client

**Purpose:** User-facing, cloud-agnostic entry point.

**Examples:** `BucketClient`, `StsClient`

**Responsibilities:**
- Accept `provider` keyword argument and resolve the implementation via the registry
- Forward calls to the driver (Layer 2)
- Manage lifecycle (`__enter__` / `__exit__` / `close()`)
- Never import from provider packages

```python
from multicloud.blob import BucketClient

with BucketClient(provider="aws", bucket="my-bucket", region="us-west-2") as client:
    client.upload("key", b"data")
```

### Layer 2 — Driver

**Purpose:** Abstract contract defining the public method signatures, input validation, and template-method hooks.

**Examples:** `BaseBlobStore`, `BaseSts`

**Responsibilities:**
- Define public methods (`upload`, `download`, `delete`) as **concrete** methods
- Validate inputs via a Validator class
- Normalize arguments (defaults, coercion)
- Delegate to abstract `do_*` hooks that providers implement
- Own the registry instance (`BLOB_REGISTRY`, `STS_REGISTRY`)
- Define frozen dataclasses for request/response types

```python
class BaseBlobStore(Provider, ABC):
    def upload(self, key, payload, *, content_type=None, ...):
        # 1. Validate input
        self._validator.validate_upload(key)
        # 2. Normalize
        payload_bytes = _coerce_to_bytes(payload)
        # 3. Delegate to provider hook
        return self.do_upload(key, payload_bytes, content_type=content_type, ...)

    @abstractmethod
    def do_upload(self, key, payload, *, content_type=None, ...) -> UploadResponse:
        ...
```

### Layer 3 — Provider

**Purpose:** Concrete, cloud-specific implementation.

**Examples:** `AwsBlobStore`, `GcpBlobStore`, `AwsSts`, `GcpSts`

**Responsibilities:**
- Override `do_*` hooks only (never override `upload`/`download`/`delete`)
- Use the native cloud SDK (boto3, google-cloud-storage, etc.)
- Implement `translate_error()` to map native exceptions to the common hierarchy
- Self-register at import time via the `@REGISTRY.register("provider_id")` decorator

```python
@BLOB_REGISTRY.register("aws")
class AwsBlobStore(BaseBlobStore):
    def do_upload(self, key, payload, *, content_type=None, ...):
        try:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=payload, ...)
        except (ClientError, BotoCoreError) as exc:
            raise self.translate_error(exc) from exc
        return UploadResponse(key=key, e_tag=..., version_id=...)

    def translate_error(self, exc):
        if isinstance(exc, ClientError):
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchBucket", "NoSuchKey"):
                return ResourceNotFoundException(str(exc))
        return UnknownException(str(exc))
```

## Provider Discovery

Each service owns a `ProviderRegistry` instance. Providers self-register at import time:

```python
# In blob_client/driver.py
BLOB_REGISTRY = ProviderRegistry(service_name="blob")

# In blob_aws/aws_blob_store.py
@BLOB_REGISTRY.register("aws")
class AwsBlobStore(BaseBlobStore):
    ...
```

The service triggers registration with guarded imports (at the bottom of `driver.py` or in the service `__init__.py`):

```python
# Guarded import triggers @BLOB_REGISTRY.register("aws") decorator
try:
    from multicloud.blob import blob_aws  # noqa: F401
except ImportError as _exc:
    BLOB_REGISTRY.record_import_error("aws", _exc)
```

## Dependency Rules

| Layer | May Import | Must NOT Import |
|-------|-----------|-----------------|
| Client | sibling driver | any provider package |
| Driver | `multicloud.common.*` | any provider package |
| Provider | sibling driver, `multicloud.common.*`, native SDKs | other provider packages |

These rules guarantee that:
- User code never sees provider classes
- Providers cannot depend on each other
- The client/driver layers remain fully cloud-agnostic

## Package Layout

Every service follows the same 3-package structure:

```
multicloud/<svc>/
├── <svc>_client/        # Layer 1 + Layer 2
│   ├── client.py        # User-facing Client class
│   └── driver.py        # BaseFoo + registry + dataclasses
├── <svc>_aws/           # Layer 3: AWS provider
├── <svc>_gcp/           # Layer 3: GCP provider
└── <svc>_ali/           # Layer 3: Alibaba provider
```
