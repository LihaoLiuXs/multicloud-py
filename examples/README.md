# Python Multi-Cloud SDK Examples

Examples are organized **by service** — one file per service, with
subcommands selecting the demo:

```
examples/
  blob/blob_example.py    # Blob storage demos
  sts/sts_example.py      # STS demos
  iam/iam_example.py      # IAM identity demos
```

The same code works against any registered provider — switch clouds by
changing the `--provider` flag only.

## Blob Storage (`blob/blob_example.py`)

| Subcommand | Description |
|---|---|
| `upload-download` | Upload an object, download it, verify content matches, delete it |
| `all` | Run every demo |

```bash
python examples/blob/blob_example.py upload-download \
    --provider aws --bucket my-bucket --region us-west-2

python examples/blob/blob_example.py upload-download \
    --provider gcp --bucket my-bucket --region us-central1
```

## Security Token Service (`sts/sts_example.py`)

| Subcommand | Description |
|---|---|
| `get-credentials` | Obtain temporary credentials from the provider's STS |
| `assume-role` | Assume an IAM role and print the resulting credentials |
| `all` | Run every demo |

```bash
python examples/sts/sts_example.py get-credentials --provider aws --region us-west-2

python examples/sts/sts_example.py assume-role --provider aws --region us-west-2 \
    --role-arn arn:aws:iam::123456789012:role/MyRole --session-name dev
```

## Prerequisites

1. Install provider dependencies:

```bash
pip install python-multicloud-sdk[aws]       # AWS S3
pip install python-multicloud-sdk[gcp]       # GCP Cloud Storage
pip install python-multicloud-sdk[alibaba]   # Alibaba OSS (placeholder)
```

2. Configure credentials:

```bash
# AWS
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...           # optional, for assumed roles

# GCP
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
export GOOGLE_CLOUD_PROJECT=my-project

# Alibaba (placeholder; not implemented)
export ALIBABA_CLOUD_ACCESS_KEY_ID=...
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
```

## Design

1. **One file per service** — clear scope, easy to discover.
2. **Subcommand dispatch** via `argparse` keeps the file from sprawling
   as new demos are added.
3. **Cloud-agnostic** — the example imports only `BucketClient` /
   `StsClient` from `multicloud.blob` / `multicloud.sts`. No
   provider-specific code in user space.
