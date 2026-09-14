# Recording STS Conformance Cassettes

This file describes how to record VCR cassettes for the STS conformance suite.
It doubles as a ready-to-run Claude Code prompt for when live credentials are
available.

---

## How recording works

- `--record-mode=rewrite` intercepts real HTTP calls and saves them as YAML
  cassettes under `tests/sts/conformance/cassettes/{aws,gcp}/`.
- `--record-mode=none` (the default) replays those cassettes. Tests whose
  cassettes have not been recorded yet are automatically skipped by the
  capability-gate logic in `BaseStsConformance`.

Run all conformance tests in replay mode (CI default, no creds needed):

```sh
pytest tests/sts/conformance --record-mode=none -v
```

---

## AWS

### Required environment variables

| Variable | Purpose |
|---|---|
| `AWS_ACCESS_KEY_ID` | **Long-term** access key (IAM user, not a role session) |
| `AWS_SECRET_ACCESS_KEY` | Matching secret key |
| `AWS_SESSION_TOKEN` | Leave **unset** for `test_get_access_token` (GetSessionToken requires non-session creds) |
| `CONFORMANCE_AWS_REGION` | Region (default: `us-west-2`) |
| `CONFORMANCE_AWS_ROLE_ARN` | Role ARN for `test_assume_role` (default: `arn:aws:iam::123456789012:role/MultiCloudPyTestRole`) |
| `CONFORMANCE_AWS_WEB_IDENTITY_ROLE_ARN` | Role ARN for `test_assume_role_with_web_identity` (must have a trust policy for the OIDC IdP) |
| `CONFORMANCE_AWS_WEB_IDENTITY_TOKEN` | A valid OIDC JWT from the trusted IdP |

> **Long-term credential caveat**: `GetSessionToken` (used by `test_get_access_token`)
> requires IAM user credentials — it **fails** when called with session credentials
> (i.e. when `AWS_SESSION_TOKEN` is set). Record this test with a long-term IAM user
> key pair and `AWS_SESSION_TOKEN` unset.

> **Web identity caveat**: `AssumeRoleWithWebIdentity` requires:
> 1. A role whose trust policy allows `sts:AssumeRoleWithWebIdentity` from your IdP.
> 2. A fresh OIDC token (JWT) from that IdP.
> Obtain the token from your identity provider and set it in
> `CONFORMANCE_AWS_WEB_IDENTITY_TOKEN` before running.

### Record commands

```sh
# All AWS conformance tests (except negative tests that need no cassette)
pytest tests/sts/conformance/test_aws_sts_conformance.py \
    --record-mode=rewrite -v

# Record only a specific test
pytest tests/sts/conformance/test_aws_sts_conformance.py \
    -k test_get_access_token --record-mode=rewrite -v

pytest tests/sts/conformance/test_aws_sts_conformance.py \
    -k test_assume_role_with_web_identity --record-mode=rewrite -v
```

### Tests that never need a cassette (AWS)

- `test_get_access_token_rejects_excess_duration` — skipped (AWS does not
  enforce the guard; `supports_duration_guard()` returns `False`)
- `test_get_access_token_with_scopes` — skipped (`supports_scoped_token()` returns `False`)
- `test_get_access_token_with_audience` — skipped (`supports_audience_token()` returns `False`)
- `test_generate_service_account_token` — skipped (`supports_generate_service_account_token()` returns `False`)
- `test_generate_service_account_token_unsupported` — always runs (no cassette; raises before HTTP)

---

## GCP

### Required environment variables

| Variable | Purpose |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to a service account JSON key file |
| `CONFORMANCE_GCP_SERVICE_ACCOUNT` | Service account email (default: `test-service-account@example-project.iam.gserviceaccount.com`) |
| `CONFORMANCE_GCP_TOKEN_AUDIENCE` | Audience for `test_get_access_token_with_audience` (default: the service account URL) |
| `CONFORMANCE_GCP_WEB_IDENTITY_ROLE` | Workload Identity Pool provider audience/resource name for `test_assume_role_with_web_identity` |
| `CONFORMANCE_GCP_WEB_IDENTITY_TOKEN` | A valid OIDC JWT from the external IdP trusted by the Workload Identity Pool |

> **Duration guard**: `test_get_access_token_rejects_excess_duration` requires
> no cassette — the GCP driver validates `duration_seconds` client-side before
> making any HTTP call. It always runs when `supports_duration_guard()` is `True`.

> **Web identity / Workload Identity Federation caveat**: You need:
> 1. A Workload Identity Pool and provider configured in the GCP project.
> 2. A trust relationship granting the external identity the `roles/iam.workloadIdentityUser` role.
> 3. A valid OIDC token from the external IdP. Set it in `CONFORMANCE_GCP_WEB_IDENTITY_TOKEN`.

### Record commands

```sh
# All GCP conformance tests
pytest tests/sts/conformance/test_gcp_sts_conformance.py \
    --record-mode=rewrite -v

# Record only specific tests
pytest tests/sts/conformance/test_gcp_sts_conformance.py \
    -k test_get_access_token_with_scopes --record-mode=rewrite -v

pytest tests/sts/conformance/test_gcp_sts_conformance.py \
    -k test_get_access_token_with_audience --record-mode=rewrite -v

pytest tests/sts/conformance/test_gcp_sts_conformance.py \
    -k test_generate_service_account_token --record-mode=rewrite -v

pytest tests/sts/conformance/test_gcp_sts_conformance.py \
    -k test_assume_role_with_web_identity --record-mode=rewrite -v
```

### Tests that never need a cassette (GCP)

- `test_get_access_token_rejects_excess_duration` — always runs (no cassette;
  error raised before HTTP); gated by `supports_duration_guard()` = `True`.

---

## Post-record checklist

After recording new cassettes, complete these steps before committing:

1. **Verify no secrets leaked** — inspect each new `*.yaml` cassette and confirm:
   - `Authorization` headers are absent or replaced with a placeholder.
   - `SessionToken`, `SecretAccessKey`, `AccessKeyId` values in XML bodies
     are replaced with `FILTERED_*` strings.
   - `access_token`, `refresh_token`, `secret` values in JSON bodies are
     replaced with `FILTERED_*` strings.
   - `WebIdentityToken` form field values are replaced with `FILTERED_WebIdentityToken`.
   - `subjectToken` JSON field values are replaced with `FILTERED_subjectToken`.
   - `token` / `id_token` in GCP response bodies are **intentionally left
     intact only when they have been replaced with synthetic test JWTs (see
     "Synthetic replay JWTs" below).

2. **Confirm replay is green** — run with no credentials:

   ```sh
   unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
   unset GOOGLE_APPLICATION_CREDENTIALS
   pytest tests/sts/conformance --record-mode=none -v
   ```

   All previously-failing-or-skipped tests should now pass (or remain skipped
   only if their cassette was intentionally not recorded).

3. **Run unit tests** to confirm no regressions:

   ```sh
   pytest tests/sts/unit -v
   ```

4. **Run lint**:

   ```sh
   black --check tests/sts/conformance
   isort --check-only tests/sts/conformance
   ```

5. Commit the new cassette files together with any code changes.

---

## Synthetic replay JWTs

Some GCP cassettes contain a `"token": "eyJ..."` value. These values are
synthetic fixtures, not tokens returned by Google:

- **Why a JWT-shaped value is needed** — the conformance tests decode the payload
  during replay to validate claims such as `aud` and `exp`.
- **Why it is safe** — the fixtures use placeholder claims, an expiration date in
  2000, and a deliberately invalid signature. They cannot authenticate to any
  service.

Affected cassettes carry an inline `# NOTE:` header pointing back to this section:

- `cassettes/gcp/TestGcpStsConformance.test_get_access_token_with_audience.yaml`
- `cassettes/gcp/TestGcpStsConformance.test_get_caller_identity.yaml`

If a secret scanner flags one of these, verify that its claims and signature still
match the documented synthetic fixture before treating it as a false positive.
