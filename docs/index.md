# Python Multi-Cloud SDK

The Python Multi-Cloud SDK is a cloud-agnostic library providing unified interfaces for cloud services across **AWS** and **GCP**. Write your code once, deploy to any cloud — switch providers by changing a single keyword argument.

## Quick Start

### Installation

```bash
# Install with all providers
pip install 'python-multicloud-sdk[all]'

# Install with specific providers only
pip install 'python-multicloud-sdk[aws]'
pip install 'python-multicloud-sdk[aws,gcp]'
```

### Hello, Multi-Cloud

```python
from multicloud.blob import BucketClient

# Upload and download — works on any cloud
with BucketClient(provider="aws", bucket="my-bucket", region="us-west-2") as client:
    client.upload("hello.txt", b"Hello, Multi-Cloud!")
    resp = client.download("hello.txt")
    print(resp.content)  # b"Hello, Multi-Cloud!"
    client.delete("hello.txt")

# Switch to GCP by changing only the provider kwarg
with BucketClient(provider="gcp", bucket="my-bucket", region="us-central1") as client:
    client.upload("hello.txt", b"Hello from GCP!")
```

### STS Example

```python
from multicloud.sts import StsClient, AssumeRoleRequest

with StsClient(provider="aws", region="us-west-2") as sts:
    identity = sts.get_caller_identity()
    print(f"Account: {identity.account}, ARN: {identity.arn}")

    resp = sts.assume_role(AssumeRoleRequest(
        role_arn="arn:aws:iam::123456789012:role/MyRole",
        role_session_name="dev",
    ))
    print(f"Temporary credentials expire: {resp.credentials.expiration}")
```

## Supported Services

| Service | Description | AWS | GCP |
|---------|-------------|:---:|:---:|
| [Blob Storage](guides/blobstore-guide.md) | Object storage (upload, download, delete) | ✅ | ✅ |
| [STS](guides/sts-guide.md) | Security Token Service (identity, assume role, credentials) | ✅ | ✅ |
| PubSub | Messaging and event streaming | 🚧 | 🚧 |
| IAM | Identity and Access Management | 🚧 | 🚧 |

✅ = Available &nbsp; 🚧 = In Progress

## Key Features

- **Cloud-agnostic API** — One interface for all providers. Switch clouds by changing `provider="aws"` to `provider="gcp"`.
- **Native SDK performance** — Uses boto3, google-cloud-storage, and ali SDKs directly. No intermediate HTTP layer.
- **Type-safe** — Full type annotations, frozen dataclasses, keyword-only arguments.
- **Consistent error handling** — All provider exceptions are translated to a common hierarchy (`SubstrateSdkException`).
- **Optional dependencies** — Install only the providers you need. Unused providers are never imported.

## Architecture

The SDK follows a strict **three-layer architecture** for every service:

1. **Client** — User-facing, cloud-agnostic API (`BucketClient`, `StsClient`)
2. **Driver** — Abstract contract with validation and template-method hooks (`BaseBlobStore`, `BaseSts`)
3. **Provider** — Concrete cloud-specific implementation (`AwsBlobStore`, `GcpBlobStore`, `AwsSts`)

Learn more in the [Design](design/index.md) section.

## Links

- [Usage Guides](guides/index.md) — Task-focused guides per service
- [Design Documentation](design/index.md) — SDK architecture and patterns
- [GitHub Repository](https://github.com/salesforce/multicloud-py)
- [Contributing](https://github.com/salesforce/multicloud-py/blob/main/CONTRIBUTING.md)
