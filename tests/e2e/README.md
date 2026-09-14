# E2E Testing Harness

Provider-agnostic end-to-end testing for the Python Multi-Cloud SDK against real cloud providers.

The same example code runs against **any provider** — only `--provider` and cloud-neutral arguments change. This demonstrates the SDK's cloud-agnostic nature while independently verifying results via raw provider SDKs and clickable console URLs.

## Architecture

```
multicloud_e2e/
├── cli.py                      — Top-level Click CLI
├── common/                     — Cross-cutting shared concerns
│   ├── harness.py              — Rich output, spinners, timing, section markers
│   ├── verifier.py             — Base ServiceVerifier ABC
│   └── console_urls.py         — Console URL builders
└── iam/                        — IAM service module
    ├── example.py              — Provider-agnostic lifecycle (ONE file)
    ├── commands.py             — Click subcommand
    └── verifiers/              — Provider-specific verification
        ├── aws.py              — raw boto3
        └── gcp.py              — raw google-cloud
```

## Prerequisites

### AWS
- Default credentials configured (`~/.aws/credentials`, env vars, or SSO)
- Credentials must be **active** (temporary STS tokens expire — refresh via your SSO/assume-role tool)
- Permissions: `iam:CreateRole`, `iam:GetRole`, `iam:DeleteRole`, `iam:UpdateRole`
- `pip install boto3`

### GCP
- Application Default Credentials (`gcloud auth application-default login`)
- Permissions: `iam.serviceAccounts.create`, `iam.serviceAccounts.get`, `iam.serviceAccounts.delete`
- `pip install google-cloud-iam`

### Python
- Python 3.8+
- `pip install click rich pyyaml`

### All dependencies in one command

```bash
pip install boto3 google-cloud-iam click rich pyyaml
```

## Quick Start

```bash
cd tests/e2e

# One-step install: SDK (from local repo) + all dependencies
pip install -e ".[local]"

# Run against GCP
python-multicloud-sdk-e2e iam --provider gcp --tenant-id my-project --region us-central1

# Run against AWS
python-multicloud-sdk-e2e iam --provider aws --tenant-id 123456789012 --region us-west-2

# Non-interactive (no pauses)
python-multicloud-sdk-e2e --no-pause iam --provider gcp --tenant-id my-project

# Leave resources for manual inspection
python-multicloud-sdk-e2e --no-cleanup iam --provider aws --tenant-id 123456789012
```

## Cloud-Neutral Arguments

| CLI Arg | Concept | AWS meaning | GCP meaning |
|---------|---------|-------------|-------------|
| `--provider` | Cloud provider | "aws" | "gcp" |
| `--identity-name` | Identity to create | IAM Role name | Service Account name |
| `--tenant-id` | Account/project | AWS Account ID | GCP Project ID |
| `--region` | Region | AWS region | GCP region |
| `--trusted-principal` | Trust config | ARN | SA email |

## Understanding Output

Each step shows three labeled sections:
- **MultiCloud SDK Call** (magenta) — our SDK invocation
- **Provider SDK Verification** (cyan) — independent check via raw provider SDK
- **Manual Verification** (yellow) — clickable console URL + interactive pause

| Symbol | Meaning |
|--------|---------|
| `✓` | Step passed |
| `✗` | Step failed |
| `🔗` | Clickable console URL |
| `(Xs)` | Per-call timing |

A summary table at the end shows all API calls, timing, and status.

## Troubleshooting

| Error | Fix |
|-------|-----|
| `NoCredentialsError` | Run `aws configure` or set env vars |
| `DefaultCredentialsError` (GCP) | Run `gcloud auth application-default login` |
| `AccessDenied` / `Forbidden` | Check IAM permissions |
| `ExpiredToken` | Refresh STS session credentials |

## Adding New Services

See [ONBOARDING.md](ONBOARDING.md) for step-by-step instructions.
