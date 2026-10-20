# Getting started

Use Python 3.10 or newer. Install only the service and provider packages your
application needs; they bring in the shared client and common dependencies.

## Install a provider

Once the distributions are available on PyPI:

```bash
# Blob Storage on AWS
pip install multicloud-py-blob-aws

# Blob Storage on GCP
pip install multicloud-py-blob-gcp
```

For development from source, install the workspace with
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/salesforce/multicloud-py.git
cd multicloud-py
uv sync --frozen --all-packages
```

Run examples from the source checkout with `uv run python example.py`.

## Configure access

Create a bucket and grant your identity permission to upload, download, and
delete objects in it. Use a bucket intended for trying the SDK.

=== "AWS"

    Configure the standard boto3 credential chain, such as an AWS profile,
    environment credentials, or a workload role. Use the bucket's region.

=== "GCP"

    Configure Application Default Credentials, such as a workload identity or
    `gcloud auth application-default login` for local development. Use your
    Cloud Storage bucket and location.

## Upload and download an object

Replace `my-bucket` and the region with your own values. This example writes
and then deletes `multicloud-py-getting-started.txt`.

```python
from multicloud.blob.blob_client import BucketClient

with BucketClient(provider="aws", bucket="my-bucket", region="us-west-2") as client:
    key = "multicloud-py-getting-started.txt"
    client.upload(key=key, payload=b"Hello, MultiCloud-Py!")
    response = client.download(key=key)
    print(response.content.decode())
    client.delete(key=key)
```

To use GCP, install `multicloud-py-blob-gcp`, select `provider="gcp"`, and
provide your bucket and location. The upload, download, and delete calls stay
the same.

## Next steps

- [Blob Storage guide](guides/blobstore-guide.md)
- [STS guide](guides/sts-guide.md)
- [Pub/Sub guide](guides/pubsub-guide.md)
- [API reference](api/index.md)
