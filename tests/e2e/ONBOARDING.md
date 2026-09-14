# Onboarding New Services to the E2E Harness

Step-by-step guide for adding a new service (e.g. PubSub, DocStore, Blob) to the E2E harness.

## Architecture

Each service is a sub-package under `src/multicloud_e2e/`:

```
multicloud_e2e/<service>/
├── __init__.py
├── example.py              — Provider-agnostic lifecycle (ONE file for all providers)
├── commands.py             — Click subcommand
└── verifiers/
    ├── __init__.py         — get_<service>_verifier(provider) dispatch
    ├── aws.py              — raw boto3/AWS SDK verification
    └── gcp.py              — raw google-cloud verification
```

## Steps

### 1. Create service directory

```bash
mkdir -p src/multicloud_e2e/<service>/verifiers
touch src/multicloud_e2e/<service>/__init__.py
touch src/multicloud_e2e/<service>/verifiers/__init__.py
```

### 2. Create verifiers

Copy from `iam/verifiers/` as a template. Each verifier implements `ServiceVerifier`:

```python
# <service>/verifiers/aws.py
from multicloud_e2e.common.verifier import ServiceVerifier

class Aws<Service>Verifier(ServiceVerifier):
    def verify_exists(self, name, tenant_id, expected_id) -> bool:
        # Use raw boto3 to confirm resource exists
        ...

    def verify_deleted(self, name, tenant_id) -> bool:
        # Use raw boto3 to confirm resource is gone
        ...

    def cleanup(self, name, tenant_id) -> None:
        # Best-effort cleanup
        ...
```

### 3. Create verifier dispatch

```python
# <service>/verifiers/__init__.py
from multicloud_e2e.common.verifier import ServiceVerifier

def get_<service>_verifier(provider: str, region: str = None) -> ServiceVerifier:
    if provider == "aws":
        from multicloud_e2e.<service>.verifiers.aws import Aws<Service>Verifier
        return Aws<Service>Verifier(region=region)
    elif provider == "gcp":
        from multicloud_e2e.<service>.verifiers.gcp import Gcp<Service>Verifier
        return Gcp<Service>Verifier(region=region)
    raise ValueError(f"No verifier for provider: {provider}")
```

### 4. Create provider-agnostic example

```python
# <service>/example.py
from multicloud.<service> import <Service>Client
from multicloud_e2e.common.harness import lifecycle_test, ...
from multicloud_e2e.<service>.verifiers import get_<service>_verifier

def run(provider, tenant_id, region, ...):
    verifier = get_<service>_verifier(provider, region)
    with <Service>Client(provider=provider, region=region) as client:
        # Same code for ANY provider — operations here
        ...
        verifier.verify_exists(...)
```

### 5. Create Click subcommand

```python
# <service>/commands.py
import click

@click.command("<service>")
@click.option("--provider", required=True, type=click.Choice(["aws", "gcp"]))
@click.option("--tenant-id", required=True)
@click.option("--region", default=None)
@click.pass_context
def <service>(ctx, provider, tenant_id, region):
    """Run <Service> lifecycle — works with any provider."""
    from multicloud_e2e.<service>.example import run
    run(provider=provider, tenant_id=tenant_id, region=region, ...)
```

### 6. Register command in cli.py

```python
# cli.py
from multicloud_e2e.<service>.commands import <service>
cli.add_command(<service>)
```

### 7. Add console URLs

In `common/console_urls.py`, add URL patterns for the new resource type.

### 8. Test

```bash
python-multicloud-sdk-e2e <service> --provider gcp --tenant-id my-project
python-multicloud-sdk-e2e <service> --provider aws --tenant-id 123456789012
```

## Naming Conventions

| Item | Pattern | Example |
|------|---------|---------|
| Service directory | `<service>/` | `iam/`, `blob/`, `sts/` |
| Example file | `<service>/example.py` | `iam/example.py` |
| Command | `<service>` | `iam`, `blob`, `sts` |
| Verifiers | `<service>/verifiers/{aws,gcp}.py` | `iam/verifiers/aws.py` |
| CLI usage | `python-multicloud-sdk-e2e <service> --provider <p>` | `...e2e iam --provider aws` |

## Claude Skill

Use `/onboard-e2e-example` to automate this process:
```
/onboard-e2e-example for blob storage
```
