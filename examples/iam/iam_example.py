#!/usr/bin/env python3
"""IAM identity examples for the Python Multi-Cloud SDK.

Demonstrates cloud-agnostic IAM identity operations. The same code works
with any provider — only the ``provider`` keyword argument changes.

Usage:
    # AWS Example
    export TENANT_ID=123456789012
    export REGION=us-west-2
    python examples/iam/iam_example.py --provider aws

    # GCP Example
    export TENANT_ID=my-gcp-project
    export REGION=us-central1
    python examples/iam/iam_example.py --provider gcp

    # Or set provider via environment variable
    export PROVIDER=aws
    python examples/iam/iam_example.py

Prerequisites:
    pip install python-multicloud-sdk[aws]  # or [gcp]
"""

import os
import sys

from multicloud.iam.iam_client import (
    CreateOptions,
    Effect,
    IamClient,
    PolicyDocument,
    Statement,
    StorageActions,
    TrustConfiguration,
)


def create_identity():
    """Create a new identity using the configured provider."""
    provider = get_provider()
    client = get_iam_client(provider)
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()

    identity_id = client.create_identity(
        identity_name,
        description="Created by Python Multi-Cloud SDK example",
        tenant_id=tenant_id,
    )

    print(f"\n=== Create Identity ===")
    print(f"Provider: {provider}")
    print(f"Identity name: {identity_name}")
    print(f"Identity ID: {identity_id}")

    client.close()
    return identity_id


def get_identity():
    """Retrieve an existing identity."""
    client = get_iam_client(get_provider())
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()

    identity_id = client.get_identity(identity_name, tenant_id=tenant_id)

    print(f"\n=== Get Identity ===")
    print(f"Identity name: {identity_name}")
    print(f"Identity ID: {identity_id}")

    client.close()
    return identity_id


def attach_inline_policy():
    """Attach an inline policy to an identity."""
    client = get_iam_client(get_provider())
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()

    policy = PolicyDocument(
        name="ReadBucketPolicy",
        statements=(
            Statement(
                sid="AllowStorageRead",
                effect=Effect.ALLOW,
                actions=(StorageActions.GET_OBJECT, StorageActions.LIST_BUCKET),
                resources=("storage://my-bucket/*",),
            ),
        ),
    )

    client.attach_inline_policy(
        identity_name, policy_document=policy, tenant_id=tenant_id
    )

    print("\n=== Attach Inline Policy ===")
    print(f'Attached policy "{policy.name}" to {identity_name}')
    print(
        f"Policy allows: storage:GetObject, storage:ListBucket on storage://my-bucket/*"
    )

    client.close()


def get_attached_policies():
    """List policies attached to an identity."""
    client = get_iam_client(get_provider())
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()

    policies = client.get_attached_policies(identity_name, tenant_id=tenant_id)

    print("\n=== Get Attached Policies ===")
    print(f"Policies attached to {identity_name}:")
    for p in policies:
        print(f"  - {p}")

    client.close()
    return policies


def get_inline_policy_details():
    """Retrieve details of a specific inline policy."""
    client = get_iam_client(get_provider())
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()
    policy_name = "ReadBucketPolicy"

    details = client.get_inline_policy_details(
        identity_name, policy_name=policy_name, tenant_id=tenant_id
    )

    print("\n=== Get Inline Policy Details ===")
    print(f'Policy "{policy_name}":')
    print(f"  {details}")

    client.close()
    return details


def remove_policy():
    """Remove a policy from an identity."""
    client = get_iam_client(get_provider())
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()
    policy_name = "ReadBucketPolicy"

    client.remove_policy(identity_name, policy_name=policy_name, tenant_id=tenant_id)

    print("\n=== Remove Policy ===")
    print(f'Removed policy "{policy_name}" from {identity_name}')

    client.close()


def delete_identity():
    """Delete an identity."""
    client = get_iam_client(get_provider())
    identity_name = os.environ.get("IDENTITY_NAME", "multicloud-example-role")
    tenant_id = get_tenant_id()

    client.delete_identity(identity_name, tenant_id=tenant_id)

    print("\n=== Delete Identity ===")
    print(f"Deleted: {identity_name}")

    client.close()


def get_iam_client(provider):
    """Create and return an IamClient instance.

    Gets configuration from environment variables.
    """
    region = os.environ.get("REGION")

    client = IamClient(
        provider=provider,
        region=region,
    )

    return client


def get_provider():
    """Get the cloud provider to use.

    Can be set via PROVIDER environment variable, otherwise defaults to "aws".
    """
    return os.environ.get("PROVIDER", "aws")


def get_tenant_id():
    """Get the tenant ID (AWS account ID or GCP project ID)."""
    tenant_id = os.environ.get("TENANT_ID")
    if not tenant_id:
        raise ValueError("TENANT_ID must be set (AWS account ID or GCP project ID)")
    return tenant_id


def main():
    """Run IAM identity examples."""
    import argparse

    parser = argparse.ArgumentParser(description="Python Multi-Cloud SDK IAM Examples")
    parser.add_argument(
        "--provider",
        choices=["aws", "gcp", "ali"],
        help="Cloud provider (can also set PROVIDER env var)",
    )
    args = parser.parse_args()

    if args.provider:
        os.environ["PROVIDER"] = args.provider

    provider = get_provider()
    print("=== Python Multi-Cloud SDK IAM Examples ===")
    print(f"Provider: {provider}")

    try:
        create_identity()
        get_identity()
        attach_inline_policy()
        get_attached_policies()
        get_inline_policy_details()
        remove_policy()
        delete_identity()

        print("\n✓ All examples completed successfully!")
        return 0

    except Exception as exc:
        print(f"\n✗ Error: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
