#!/usr/bin/env python3
"""STS examples for the Python Multi-Cloud SDK.

Demonstrates cloud-agnostic STS operations. The same code works with any provider.

Usage:
    # AWS Example
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    python examples/sts/sts_example.py --provider aws

    # GCP Example
    export GOOGLE_APPLICATION_CREDENTIALS=...
    python examples/sts/sts_example.py --provider gcp

    # AWS Assume Role
    python examples/sts/sts_example.py --provider aws \
        --role arn:aws:iam::123456789012:role/MyRole

    # GCP Assume Role (service account impersonation)
    python examples/sts/sts_example.py --provider gcp \
        --role target-sa@my-project.iam.gserviceaccount.com

    # Audience-scoped token (GCP: generates an ID token for the given audience)
    python examples/sts/sts_example.py --provider gcp \
        --audience https://my-service.example.com

    # Scoped access token (GCP: generates an access token for specific OAuth scopes)
    python examples/sts/sts_example.py --provider gcp \
        --scopes https://www.googleapis.com/auth/cloud-platform

    # Service account impersonation token (GCP only)
    python examples/sts/sts_example.py --provider gcp \
        --target-principal target-sa@my-project.iam.gserviceaccount.com \
        --scopes https://www.googleapis.com/auth/cloud-platform

Every flag also has an environment-variable fallback (e.g. ROLE, AUDIENCE,
SCOPES, TARGET_PRINCIPAL) so existing invocations keep working. Run with
``--help`` to see all available flags.

    # Assume Role With Web Identity (federated OIDC sign-in)
    export WEB_IDENTITY_TOKEN=<oidc-jwt>
    # AWS: ROLE = IAM role ARN; GCP: ROLE = workload identity provider resource
    export ROLE=arn:aws:iam::123456789012:role/WebRole
    python examples/sts/sts_example.py --provider aws

Prerequisites:
    pip install python-multicloud-sdk[aws]   # for AWS
    pip install python-multicloud-sdk[gcp]   # for GCP
"""

import argparse
import os
import sys

from multicloud.sts.sts_client import (
    AvailabilityCondition,
    CredentialScope,
    ScopeRule,
    StsClient,
)


def get_caller_identity(client: StsClient) -> None:
    """Get the current caller identity."""
    print("\n=== Get Caller Identity ===")
    identity = client.get_caller_identity()

    print(f"  User ID:             {identity.user_id}")
    print(f"  Cloud Resource Name: {identity.cloud_resource_name[:60]}...")
    print(f"  Account ID:          {identity.account_id}")


def get_access_token(client: StsClient, args: argparse.Namespace) -> None:
    """Get a fresh access token."""
    print("\n=== Get Access Token ===")
    creds = client.get_access_token(duration_seconds=args.duration_seconds)

    print(f"  Access Key ID:     {creds.access_key_id[:20] or '(empty)'}...")
    print(f"  Access Key Secret: {creds.access_key_secret[:8] or '(empty)'}...")
    print(f"  Security Token:    {creds.security_token[:30]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def get_access_token_with_audience(client: StsClient, args: argparse.Namespace) -> None:
    """Get an audience-scoped token.

    Behaviour is provider-specific — see the provider's do_get_access_token
    docstring for details (e.g. GCP generates an ID token via generateIdToken).
    """
    audience = args.audience
    if not audience:
        print("\n=== Get Access Token (audience) ===")
        print("  Skipped: --audience not provided")
        print("  Set --audience to a target service URL or identifier")
        return

    print("\n=== Get Access Token (audience) ===")
    print(f"  Audience: {audience}")
    creds = client.get_access_token(audience=audience)

    print(f"  Security Token:    {creds.security_token[:40]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def get_access_token_with_scopes(client: StsClient, args: argparse.Namespace) -> None:
    """Get a scoped access token.

    Behaviour is provider-specific — see the provider's do_get_access_token
    docstring for details (e.g. GCP generates a scoped token via generateAccessToken).
    """
    if not args.scopes:
        print("\n=== Get Access Token (scopes) ===")
        print("  Skipped: --scopes not provided")
        print("  Set --scopes to a comma-separated list of OAuth scope URLs")
        return

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    print("\n=== Get Access Token (scopes) ===")
    print(f"  Scopes: {scopes}")
    creds = client.get_access_token(scopes=scopes)

    print(f"  Security Token:    {creds.security_token[:30]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def generate_service_account_token(client: StsClient, args: argparse.Namespace) -> None:
    """Generate a token for a target service account via impersonation.

    Behaviour is provider-specific — see the provider's
    do_generate_service_account_token docstring for details
    (e.g. GCP calls IAM Credentials generateAccessToken; AWS raises
    UnsupportedOperationException).
    """
    target_principal = args.target_principal

    if not target_principal or not args.scopes:
        print("\n=== Generate Service Account Token ===")
        print("  Skipped: --target-principal and/or --scopes not provided")
        print("  Set --target-principal to the service account to impersonate")
        print("  Set --scopes to a comma-separated list of OAuth scope URLs")
        return

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    lifetime = args.lifetime

    print("\n=== Generate Service Account Token ===")
    print(f"  Target Principal: {target_principal}")
    print(f"  Scopes:           {scopes}")
    print(f"  Lifetime:         {lifetime}s")

    creds = client.generate_service_account_token(
        target_principal,
        scopes=scopes,
        lifetime=lifetime,
    )

    print(f"  Security Token:    {creds.security_token[:30]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def assume_role(client: StsClient, args: argparse.Namespace) -> None:
    """Assume a role and get temporary credentials."""
    role = args.role
    session_name = args.session_name
    expiration = args.expiration

    if not role:
        print("\n=== Assume Role ===")
        print("  Skipped: --role not provided")
        print("  Set --role to an AWS ARN or GCP service account email")
        return

    print("\n=== Assume Role ===")
    print(f"  Role:         {role}")
    print(f"  Session Name: {session_name}")
    print(f"  Expiration:   {expiration} seconds")

    creds = client.assume_role(
        role,
        session_name=session_name,
        expiration=expiration,
    )

    print(f"  Assumed role successfully!")
    print(f"  Access Key ID:     {creds.access_key_id[:20] or '(empty)'}...")
    print(f"  Access Key Secret: {creds.access_key_secret[:8] or '(empty)'}...")
    print(f"  Security Token:    {creds.security_token[:30]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def assume_role_with_scope(client: StsClient, args: argparse.Namespace) -> None:
    """Assume a role with credential scoping (downscoped credentials)."""
    role = args.role
    bucket = args.scope_bucket

    if not role or not bucket:
        print("\n=== Assume Role with Credential Scope ===")
        print("  Skipped: --role and --scope-bucket are both required")
        print("  Set --role to an AWS ARN or GCP service account email")
        print("  Set --scope-bucket to a storage bucket name (e.g. my-bucket)")
        return

    print("\n=== Assume Role with Credential Scope ===")
    print(f"  Role:   {role}")
    print(f"  Bucket: {bucket}")

    scope = CredentialScope(
        rules=(
            ScopeRule(
                available_resource=f"storage://{bucket}",
                available_permissions=("storage:GetObject", "storage:PutObject"),
                availability_condition=AvailabilityCondition(
                    resource_prefix=f"storage://{bucket}/scoped-prefix/",
                    title="Limit to scoped-prefix folder",
                ),
            ),
        )
    )

    print(f"  Scope:  storage://{bucket} [GetObject, PutObject] prefix=scoped-prefix/")

    creds = client.assume_role(
        role,
        session_name="scoped-session",
        credential_scope=scope,
    )

    print(f"  Assumed role with scope successfully!")
    print(f"  Access Key ID:     {creds.access_key_id[:20] or '(empty)'}...")
    print(f"  Access Key Secret: {creds.access_key_secret[:8] or '(empty)'}...")
    print(f"  Security Token:    {creds.security_token[:30]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def assume_role_with_web_identity(client: StsClient) -> None:
    """Exchange a web identity (OIDC) token for temporary credentials."""
    role = os.environ.get("WEB_IDENTITY_ROLE") or os.environ.get("ROLE")
    token = os.environ.get("WEB_IDENTITY_TOKEN")
    session_name = os.environ.get("SESSION_NAME", "multicloud-web-identity")
    expiration = int(os.environ.get("EXPIRATION", "3600"))

    print("\n=== Assume Role With Web Identity ===")
    if not role or not token:
        print("  Skipped: set WEB_IDENTITY_TOKEN and a role")
        print("  AWS: ROLE = IAM role ARN whose trust policy allows the OIDC issuer")
        print("  GCP: ROLE = workload identity federation provider resource name")
        return

    creds = client.assume_role_with_web_identity(
        role,
        token,
        session_name=session_name,
        expiration=expiration,
    )

    print("  Federated sign-in successful!")
    print(f"  Access Key ID:     {creds.access_key_id[:20] or '(empty)'}...")
    print(f"  Access Key Secret: {creds.access_key_secret[:8] or '(empty)'}...")
    print(f"  Security Token:    {creds.security_token[:30]}...")
    if creds.expiration:
        print(f"  Expiration:        {creds.expiration}")


def main() -> int:
    """Run STS examples."""
    parser = argparse.ArgumentParser(description="Python Multi-Cloud SDK STS Examples")
    parser.add_argument(
        "--provider",
        choices=["aws", "gcp"],
        default=os.environ.get("PROVIDER", "aws"),
        help="Cloud provider (default: aws, or set PROVIDER env var)",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("REGION"),
        help="Region (default: provider default)",
    )
    parser.add_argument(
        "--role",
        default=os.environ.get("ROLE"),
        help="Role to assume: AWS ARN or GCP service account email "
        "(or set ROLE env var)",
    )
    parser.add_argument(
        "--session-name",
        default=os.environ.get("SESSION_NAME", "multicloud-sdk-session"),
        help="Session name for assume_role (or set SESSION_NAME env var)",
    )
    parser.add_argument(
        "--expiration",
        type=int,
        default=int(os.environ.get("EXPIRATION", "3600")),
        help="Assume-role credential lifetime in seconds (or EXPIRATION env var)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=int(os.environ.get("DURATION_SECONDS", "900")),
        help="Access-token lifetime in seconds (or DURATION_SECONDS env var)",
    )
    parser.add_argument(
        "--audience",
        default=os.environ.get("AUDIENCE"),
        help="Target audience for an audience-scoped token (or AUDIENCE env var)",
    )
    parser.add_argument(
        "--scopes",
        default=os.environ.get("SCOPES"),
        help="Comma-separated OAuth scope URLs (or SCOPES env var)",
    )
    parser.add_argument(
        "--target-principal",
        default=os.environ.get("TARGET_PRINCIPAL"),
        help="Service account to impersonate (or TARGET_PRINCIPAL env var)",
    )
    parser.add_argument(
        "--lifetime",
        type=int,
        default=int(os.environ.get("LIFETIME", "3600")),
        help="Service-account-token lifetime in seconds (or LIFETIME env var)",
    )
    parser.add_argument(
        "--scope-bucket",
        default=os.environ.get("SCOPE_BUCKET"),
        help="Storage bucket name for credential scoping (or SCOPE_BUCKET env var)",
    )
    args = parser.parse_args()

    print("=== Python Multi-Cloud SDK STS Examples ===")
    print(f"Provider: {args.provider}")
    if args.region:
        print(f"Region:   {args.region}")

    failures = []
    with StsClient(provider=args.provider, region=args.region) as client:
        try:
            get_caller_identity(client)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("get_caller_identity")

        try:
            get_access_token(client, args)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("get_access_token")

        try:
            get_access_token_with_audience(client, args)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("get_access_token_with_audience")

        try:
            get_access_token_with_scopes(client, args)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("get_access_token_with_scopes")

        try:
            generate_service_account_token(client, args)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("generate_service_account_token")

        try:
            assume_role(client, args)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("assume_role")

        try:
            assume_role_with_scope(client, args)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("assume_role_with_scope")

        try:
            assume_role_with_web_identity(client)
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            failures.append("assume_role_with_web_identity")

    if failures:
        print(f"\n✗ {len(failures)} failed: {', '.join(failures)}")
        return 1
    else:
        print("\n✓ All examples completed successfully!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
