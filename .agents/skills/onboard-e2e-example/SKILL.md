---
name: onboard-e2e-example
description: Use when adding a service or operation lifecycle to the MultiCloud-Py live E2E harness under tests/e2e, with independent native-SDK verification.
---

# Add a service to the E2E harness

Read `AGENTS.md`, `tests/e2e/ONBOARDING.md`, `tests/e2e/README.md`, and
`tests/e2e/pyproject.toml`. This harness uses real cloud resources and is
excluded from normal pytest collection. Adding code does not require running
live tests; establish the intended test account, resources, credentials, and
authorization before invoking a lifecycle against a provider.

## Inspect the existing pattern

Under `tests/e2e/src/multicloud_e2e/`, read:

- `common/harness.py`, `common/verifier.py`, and `common/console_urls.py`.
- `iam/example.py`, `iam/commands.py`, and `iam/verifiers/`.
- `cli.py` for subcommand registration.

Identify the requested service, operations, cloud-neutral resource arguments,
native verification calls, and console links. Infer these from the task and
existing implementation where possible; ask for missing resource or behavior
decisions rather than requesting credentials as plain text.

## Implement the lifecycle

Create the service's `__init__.py`, `example.py`, `commands.py`, and
`verifiers/__init__.py` plus verifier modules for implemented providers.
Keep one cloud-agnostic lifecycle example per service, with native SDK checks
confined to `verifiers/`. Follow `ServiceVerifier` and the shared output and
cleanup helpers. Use current SDK imports, such as
`from multicloud.blob.blob_client import BucketClient`.

Register the Click command in `cli.py`; use cloud-neutral option names such
as `--tenant-id` and resource names. Add console URL helpers when applicable.
Install new dependencies in this harness's `pyproject.toml`, preserving the
root SDK workspace boundary. Use an isolated environment and uv; follow the
existing local SDK dependency layout and test installation if changing it.

Verify each create/update/read/delete result using the native SDK. Cleanup
must work on partial failures and stay within resources created for the run.
Do not add an unimplemented provider to a CLI choice as if it were supported.

## Validate and report

With the harness installed in its isolated environment, inspect command help:

```bash
python-multicloud-sdk-e2e --help
python-multicloud-sdk-e2e SERVICE --help
```

Use mocks for command wiring and verification behavior when possible. Only
run live lifecycle commands after establishing their target and authorization;
never infer a production tenant or silently use default cloud resources.

Update onboarding instructions and report files changed, the command to run,
how neutral arguments map to supported providers, validation performed, and
whether live verification actually ran. Do not describe help output or a
mocked test as successful live-cloud validation.
