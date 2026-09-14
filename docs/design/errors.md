# Exception Handling

The SDK provides a unified exception hierarchy so that user code can write a single `except` clause that works across all cloud providers.

## Exception Hierarchy

```
SubstrateSdkException (root)
├── InvalidArgumentException
├── ResourceNotFoundException
├── ResourceExistsException
├── UnAuthorizedException
├── FailedPreconditionException
├── UnsupportedOperationException
└── UnknownException
```

All exceptions are defined in `multicloud.common.exceptions`.

## Exception Types

| Exception | HTTP Analog | When Raised |
|-----------|:-----------:|-------------|
| `InvalidArgumentException` | 400 | Caller provided invalid input (empty key, bad type) |
| `ResourceNotFoundException` | 404 | Target resource (bucket, object, role) does not exist |
| `ResourceExistsException` | 409 | Attempted to create a resource that already exists |
| `UnAuthorizedException` | 401/403 | Credentials missing, expired, or insufficient permissions |
| `FailedPreconditionException` | 412 | Operation rejected because a precondition was not met |
| `UnsupportedOperationException` | 501 | Operation not supported by this provider |
| `UnknownException` | 500 | Catch-all for errors that do not map to a specific type |

## Error Translation Pattern

Every provider must implement `translate_error()` — an instance method that maps native SDK exceptions to the common hierarchy:

```python
class AwsBlobStore(BaseBlobStore):
    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS SDK exceptions to common hierarchy."""
        if isinstance(exc, SubstrateSdkException):
            return exc  # Already translated
        if isinstance(exc, ClientError):
            code = exc.response["Error"]["Code"]
            if code in ("NoSuchBucket", "NoSuchKey"):
                return ResourceNotFoundException(str(exc))
            if code == "AccessDenied":
                return UnAuthorizedException(str(exc))
            if code == "BucketAlreadyOwnedByYou":
                return ResourceExistsException(str(exc))
        if isinstance(exc, NoCredentialsError):
            return UnAuthorizedException(str(exc))
        return UnknownException(str(exc))
```

### Key Rules

1. **Instance method** — `translate_error(self, exc)` is enforced via `@abstractmethod` on the `Provider` base class
2. **Preserve the original exception** — Always use `raise translated from exc`
3. **Service-specific mappings** — Blob error codes differ from STS error codes, so each service's providers implement their own translation
4. **Check if already translated** — Return `exc` unchanged if it's already a `SubstrateSdkException`

## Usage in Provider Hooks

Providers catch native exceptions in their `do_*` methods and translate them:

```python
class AwsBlobStore(BaseBlobStore):
    def do_upload(self, key, payload, **kwargs):
        try:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=payload)
        except (ClientError, BotoCoreError) as exc:
            raise self.translate_error(exc) from exc
        return UploadResponse(key=key, ...)
```

## User-Facing Error Handling

Users catch exceptions from the common hierarchy — never provider-specific exceptions:

```python
from multicloud.blob import BucketClient
from multicloud.common.exceptions import (
    ResourceNotFoundException,
    UnAuthorizedException,
    SubstrateSdkException,
)

with BucketClient(provider="aws", bucket="b", region="us-west-2") as client:
    try:
        client.download("missing-key.txt")
    except ResourceNotFoundException:
        print("Object not found")
    except UnAuthorizedException:
        print("Access denied — check credentials")
    except SubstrateSdkException:
        print("Other SDK error")
```

## Input Validation

Input validation happens at Layer 2 (Driver) before the provider hook is called. Validation errors raise `InvalidArgumentException`:

```python
# In BaseBlobStore.upload():
if not key or not key.strip():
    raise InvalidArgumentException("key must be a non-empty string")
```

This ensures consistent validation regardless of which provider is in use.
