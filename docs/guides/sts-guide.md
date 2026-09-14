# Security Token Service (STS) Guide

The STS module provides cloud-agnostic identity and credential management. Use it to verify caller identity, assume roles for cross-account access, exchange federated (OIDC) tokens, and mint access or service-account tokens. A companion `StsUtilities` class signs identity requests offline for cloud-native auth brokers.

## Creating a Client

```python
from multicloud.sts import StsClient

# AWS
with StsClient(provider="aws", region="us-west-2") as sts:
    ...

# Google Cloud
with StsClient(provider="gcp") as sts:
    ...
```

### Constructor Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `provider` | `str` | ✅ | Provider id: `"aws"` or `"gcp"` |
| `region` | `str` | | Region, interpreted per provider (not required for GCP) |
| `endpoint` | `str` | | Endpoint URL override |
| `properties` | `Mapping[str, Any]` | | Provider-specific extras passed through to the provider |

All parameters are **keyword-only** (enforced by `*` separator).

### With a Custom Endpoint

```python
with StsClient(
    provider="aws",
    region="us-west-2",
    endpoint="https://sts.us-west-2.amazonaws.com",
) as sts:
    ...
```

!!! note
    Credentials are discovered automatically from environment variables, config files, or IAM/ADC. The SDK never accepts explicit credential parameters in the constructor.

## Getting Caller Identity

Retrieve identity information for the currently authenticated principal.

```python
from multicloud.sts import StsClient

with StsClient(provider="aws", region="us-west-2") as sts:
    identity = sts.get_caller_identity()
    print(f"Account:  {identity.account_id}")
    print(f"Resource: {identity.cloud_resource_name}")
    print(f"User ID:  {identity.user_id}")
```

`get_caller_identity` accepts an optional `aud` keyword; some providers use it for audience-scoped identity tokens, others ignore it.

The returned `CallerIdentity` is a frozen dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | `str` | Unique identifier of the calling entity |
| `cloud_resource_name` | `str` | Cloud resource identifier / identity proof |
| `account_id` | `str` | Cloud account/project that owns the identity |

## Assuming a Role

Assume a role and receive temporary credentials for cross-account or elevated-privilege access. Returns an `StsCredentials`.

```python
from multicloud.sts import StsClient

with StsClient(provider="aws", region="us-west-2") as sts:
    creds = sts.assume_role(
        "arn:aws:iam::123456789012:role/CrossAccountRole",
        session_name="my-session",
        expiration=3600,
    )
    print(f"Access Key ID: {creds.access_key_id}")
    print(f"Token:         {creds.security_token}")
    print(f"Expires:       {creds.expiration}")
```

### Assume Role Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `role` | `str` | ✅ | Cloud resource name to assume |
| `session_name` | `str` | | Session identifier; an SDK default is applied when empty |
| `expiration` | `int` | | Duration in seconds; default `3600` (must be positive) |
| `external_id` | `str` | | External ID for cross-account access |
| `policy` | `str` | | Inline policy to scope down permissions |
| `tags` | `Mapping[str, str]` | | Session tags |
| `credential_scope` | `CredentialScope` | | Optional scope to downscope the returned credentials |

### StsCredentials Fields

`StsCredentials` is a frozen dataclass returned by all credential operations. Which fields are populated depends on the operation and provider; provider-agnostic code should rely on `security_token` (and `expiration` when present).

| Field | Type | Description |
|-------|------|-------------|
| `access_key_id` | `str` | Access key identifier, or empty for bearer/token-only credentials |
| `access_key_secret` | `str` | Secret access key, or empty for bearer/token-only credentials |
| `security_token` | `str` | Session/bearer token (always populated) |
| `expiration` | `datetime \| None` | When the credentials expire, or `None` if the provider reports none |

## Federated Sign-In (Web Identity)

Exchange a web identity (OIDC/JWT) token minted by an external identity provider for short-lived cloud credentials — no long-lived cloud keys required.

```python
from multicloud.sts import StsClient

with StsClient(provider="aws", region="us-west-2") as sts:
    creds = sts.assume_role_with_web_identity(
        "arn:aws:iam::123456789012:role/WebIdentityRole",
        web_identity_token=oidc_jwt,
        session_name="federated-session",
        expiration=3600,
    )
    print(creds.security_token)
```

### Web Identity Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `role` | `str` | ✅ | Cloud resource identifier to assume (interpreted per provider) |
| `web_identity_token` | `str` | ✅ | OIDC/JWT token proving external identity |
| `session_name` | `str` | | Session identifier (provider support varies) |
| `expiration` | `int` | | Requested duration in seconds; default `3600` (provider support varies) |

On AWS this maps to `AssumeRoleWithWebIdentity`. On GCP it performs an RFC 8693 token exchange via Workload Identity Federation; the federated access token is returned in `security_token`.

## Getting an Access Token

Retrieve a fresh access token. Pass `audience` for an audience-scoped token or `scopes` for an OAuth-scoped token — the two are mutually exclusive.

```python
from multicloud.sts import StsClient

with StsClient(provider="gcp") as sts:
    # OAuth-scoped token
    token = sts.get_access_token(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    # ...or an audience-scoped token
    token = sts.get_access_token(audience="my-target-audience")

    print(token.security_token)
```

### Access Token Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `audience` | `str` | | Target audience for an audience-scoped token (mutually exclusive with `scopes`) |
| `scopes` | `list[str]` | | OAuth scopes for a scoped token (mutually exclusive with `audience`) |
| `duration_seconds` | `int` | | Requested duration; providers may cap or ignore it |

!!! note
    `audience` and `scopes` are GCP-only parameters. The AWS implementation of `get_access_token` maps to `GetSessionToken`, which does not support either parameter — both are silently ignored when calling with `provider="aws"`.

## Service Account Tokens

Generate a token for a target service account via impersonation.

```python
from multicloud.sts import StsClient

with StsClient(provider="gcp") as sts:
    token = sts.generate_service_account_token(
        "my-sa@my-project.iam.gserviceaccount.com",
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=3600,
    )
    print(token.security_token)
```

### Service Account Token Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `target_principal` | `str` | ✅ | Service account to impersonate (format per provider) |
| `scopes` | `list[str]` | ✅ | OAuth scopes to request on the generated token |
| `lifetime` | `int` | | Token lifetime in seconds; default `3600` (range/behaviour per provider) |

!!! note
    `generate_service_account_token` is supported on GCP. On AWS it raises `UnsupportedOperationException`.

## Signed Auth Requests (Cloud-Native Auth)

`StsUtilities` is a companion to `StsClient` for **offline request signing**. Instead of calling the provider itself, it produces a *signed-but-unsent* identity request. You hand that request to another system — typically an identity broker such as HashiCorp Vault's AWS-IAM auth method — which replays it to the provider to verify who you are, then issues its own token or secret in return.

```python
from multicloud.sts.sts_client import StsUtilities

with StsUtilities(provider="aws", region="us-west-2") as utils:
    signed = utils.create_signed_auth_request(
        # Extra headers are folded into the signature, so the broker can
        # bind them to the verified identity (e.g. Vault's server-id header).
        custom_headers={"X-Vault-AWS-IAM-Server-ID": "vault.example.com"},
    )

    # Forward these fields to the broker; it replays the request as-is.
    print(signed.method)   # "POST"
    print(signed.url)      # the provider's identity endpoint
    print(signed.headers)  # includes the signature and any custom headers
    print(signed.body)     # request body bytes
```

On AWS this signs a `GetCallerIdentity` request with SigV4 using the caller's
discovered credentials; the SDK never sends that request itself — replaying it
is the broker's job. Resolving the signing credentials can still require a
network call (an `assume_role`/web-identity overrider, or an IMDS/SSO chain, is
fetched lazily at signing time); only static credentials sign with zero network
I/O. Because `custom_headers` are part of the signed headers, the broker can
trust that a value like `X-Vault-AWS-IAM-Server-ID` was bound at signing time
and not injected during replay.

### Constructor Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `provider` | `str` | ✅ | Provider id: `"aws"` |
| `region` | `str` | | Region, interpreted per provider; required by AWS for signing |
| `credentials_overrider` | `CredentialsOverrider` | | Override default credential resolution |
| `properties` | `Mapping[str, Any]` | | Provider-specific extras passed through to the provider |

All parameters are **keyword-only**.

### create_signed_auth_request Parameters

| Parameter | Type | Required | Description |
|-----------|------|:--------:|-------------|
| `custom_headers` | `Mapping[str, str]` | | Extra headers folded into the signature so a broker can bind them to the identity |

### SignedAuthRequest Fields

`SignedAuthRequest` is a frozen dataclass — the signed request to forward to the broker.

| Field | Type | Description |
|-------|------|-------------|
| `method` | `str` | HTTP method of the signed request |
| `url` | `str` | Fully-qualified request URL |
| `headers` | `Mapping[str, str]` | Signed request headers (single value per name); replayable identity proof — treat as a secret |
| `body` | `bytes` | Request body bytes |
| `credentials` | `StsCredentials` | Signer identity (key id + session token); the long-term signing secret is omitted |

!!! note
    `create_signed_auth_request` is supported on AWS. On GCP it raises `UnsupportedOperationException` — GCP has no equivalent client-signed identity request.

## Provider Support

| Operation | AWS | GCP |
|-----------|-----|-----|
| `get_caller_identity` | Yes | Yes |
| `assume_role` | Yes | Yes |
| `assume_role_with_web_identity` | Yes | Yes |
| `get_access_token` | Yes (ignores `audience` and `scopes`) | Yes |
| `generate_service_account_token` | No (`UnsupportedOperationException`) | Yes |
| `StsUtilities.create_signed_auth_request` | Yes | No (`UnsupportedOperationException`) |

## Complete Example

A full round-trip: verify the current identity, assume a role, and inspect the temporary credentials.

```python
from multicloud.sts import StsClient

with StsClient(provider="aws", region="us-west-2") as sts:
    # Verify current identity
    identity = sts.get_caller_identity()
    print(f"Caller: {identity.cloud_resource_name}")

    # Assume a cross-account role
    creds = sts.assume_role(
        "arn:aws:iam::123456789012:role/CrossAccountRole",
        session_name="demo",
        expiration=900,
    )

    # Use the temporary credentials
    assert creds.security_token
    print(f"Assumed role; token expires: {creds.expiration}")
```

## Switching Providers

The same code works across clouds — change only the `provider` (and `region` where applicable):

```python
from multicloud.sts import StsClient

providers = [
    {"provider": "aws", "region": "us-west-2"},
    {"provider": "gcp"},
]

for config in providers:
    with StsClient(**config) as sts:
        identity = sts.get_caller_identity()
        print(f"[{config['provider']}] Account: {identity.account_id}")
```

## Error Handling

All STS operations raise exceptions from the common `multicloud.common.exceptions` hierarchy:

```python
from multicloud.sts import StsClient
from multicloud.common.exceptions import (
    UnAuthorizedException,
    InvalidArgumentException,
    UnsupportedOperationException,
    SubstrateSdkException,
)

with StsClient(provider="aws", region="us-west-2") as sts:
    try:
        creds = sts.assume_role(
            "arn:aws:iam::999:role/DoesNotExist",
            session_name="test",
        )
    except UnAuthorizedException as e:
        print(f"Not authorized: {e}")
    except InvalidArgumentException as e:
        print(f"Bad input: {e}")
    except SubstrateSdkException as e:
        print(f"SDK error: {e}")
```

STS operations map provider errors onto this subset of the common
`multicloud.common.exceptions` hierarchy:

| Exception | When Raised |
|-----------|-------------|
| `UnAuthorizedException` | Credentials missing/expired, insufficient permissions, or the target role cannot be assumed (e.g. missing or denied) |
| `InvalidArgumentException` | Invalid parameters (e.g. empty role, non-positive duration, both `audience` and `scopes`) |
| `UnsupportedOperationException` | Operation not supported by the provider (e.g. `generate_service_account_token` on AWS) |
| `UnknownException` | Unmapped provider error |

Other exception types in the common hierarchy (such as `ResourceNotFoundException`)
are not currently emitted by STS operations; catch `SubstrateSdkException` for a
provider-agnostic catch-all.

## Client Lifecycle

`StsClient` supports the context manager protocol for deterministic cleanup:

```python
# Preferred — automatic cleanup
with StsClient(provider="aws", region="us-west-2") as sts:
    identity = sts.get_caller_identity()

# Manual cleanup
sts = StsClient(provider="aws", region="us-west-2")
try:
    identity = sts.get_caller_identity()
finally:
    sts.close()
```
