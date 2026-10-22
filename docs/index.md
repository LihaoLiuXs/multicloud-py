# MultiCloud-Py

## Write once, deploy to any supported cloud

MultiCloud-Py is a cloud-agnostic Python SDK providing unified interfaces for
cloud services. Build applications against a consistent API and select a cloud
provider when you create a client.

[Get started](getting-started.md){ .md-button .md-button--primary }
[API reference](api/index.md){ .md-button }

## Key features

- **Cloud-agnostic APIs:** use the same client interface across supported providers.
- **Provider isolation:** install only the service and provider packages you need.
- **Native cloud SDKs:** provider implementations use the underlying cloud SDKs.
- **Consistent errors:** handle a shared exception hierarchy in application code.
- **Conformance tests:** shared tests verify behavior across provider implementations.

## Supported services

| Service | AWS | GCP | Documentation |
| --- | --- | --- | --- |
| Blob Storage | S3 | Cloud Storage | [Usage guide](guides/blobstore-guide.md) |
| Security Token Service | STS | IAM credentials | [Usage guide](guides/sts-guide.md) |
| Pub/Sub | SNS / SQS | Pub/Sub | [Usage guide](guides/pubsub-guide.md) |
| IAM | IAM | IAM | [Usage guide](guides/iam-guide.md) |
| Document Store | DynamoDB | Firestore | [API reference](api/docstore.md) |

Ali Cloud distributions currently reserve package names; their provider
implementations are planned. See the [roadmap](roadmap.md) for development plans.

## One API across clouds

```python
from multicloud.blob.blob_client import BucketClient

with BucketClient(provider="aws", bucket="my-bucket", region="us-west-2") as client:
    client.upload(key="hello.txt", payload=b"Hello, MultiCloud-Py!")
    response = client.download(key="hello.txt")
    print(response.content)
```

Select `provider="gcp"` and use your Cloud Storage bucket and region to run
the same operations on GCP. Configure credentials for the selected provider
before running the example.

## Explore the SDK

- [Getting started](getting-started.md): install a provider and run your first example.
- [Usage guides](guides/index.md): task-focused walkthroughs for cloud services.
- [Design](design/index.md): client, driver, and provider architecture.
- [API reference](api/index.md): signatures, types, and source docstrings.
- [Examples](https://github.com/salesforce/multicloud-py/tree/main/examples): working SDK examples.

## Community

[Meet the team](team.md) and reach out with questions about the SDK or contributing.

- [GitHub repository](https://github.com/salesforce/multicloud-py)
- [Issues and questions](https://github.com/salesforce/multicloud-py/issues)
- [Contributing](https://github.com/salesforce/multicloud-py/blob/main/CONTRIBUTING.md)
- [Releases](https://github.com/salesforce/multicloud-py/releases)
- [MultiCloudJ](https://opensource.salesforce.com/multicloudj/): the Java SDK.

MultiCloud-Py is maintained by Salesforce and released under the
[Apache License 2.0](https://github.com/salesforce/multicloud-py/blob/main/LICENSE.txt).
