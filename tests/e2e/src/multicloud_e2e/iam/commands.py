"""Click subcommand for IAM E2E testing."""

import click


@click.command("iam")
@click.option(
    "--provider",
    required=True,
    type=click.Choice(["aws", "gcp"]),
    help="Cloud provider.",
)
@click.option(
    "--identity-name", default=None, help="Identity name (auto-generated if omitted)."
)
@click.option(
    "--tenant-id",
    required=True,
    help="Account/project ID (AWS account ID or GCP project ID).",
)
@click.option("--region", default=None, help="Region (e.g. us-west-2, us-central1).")
@click.option(
    "--trusted-principal", default=None, help="Principal for trust configuration."
)
@click.option(
    "--creds-type",
    default=None,
    type=click.Choice(["session", "assume-role", "web-identity"]),
    help="Exercise CredentialsOverrider instead of default credentials.",
)
@click.option(
    "--assume-role-target",
    default=None,
    help="Role to assume/impersonate: ARN (AWS) or service-account email (GCP). "
    "Required for --creds-type assume-role / web-identity.",
)
@click.option(
    "--web-identity-token-file",
    default=None,
    help="File whose contents are the OIDC web-identity token "
    "(--creds-type web-identity).",
)
@click.option(
    "--session-name",
    default=None,
    help="Role session name for assume-role (optional).",
)
@click.option(
    "--duration-seconds",
    default=None,
    type=int,
    help="Credential lifetime in seconds for assume-role (optional).",
)
@click.pass_context
def iam(
    ctx,
    provider,
    identity_name,
    tenant_id,
    region,
    trusted_principal,
    creds_type,
    assume_role_target,
    web_identity_token_file,
    session_name,
    duration_seconds,
):
    """Run IAM identity lifecycle — works with any provider.

    \b
    Creates, retrieves, and deletes an identity using the cloud-agnostic
    IamClient. The same code runs against AWS (IAM Roles) or GCP (Service
    Accounts) — only --provider changes.

    \b
    Required:
      --provider     "aws" or "gcp"
      --tenant-id    AWS account ID or GCP project ID

    \b
    Optional:
      --identity-name   Name for the test identity (auto-generated if omitted)
      --region          Cloud region
      --trusted-principal  ARN (AWS) or SA email (GCP) for trust config

    \b
    Credential override (exercises CredentialsOverrider):
      --creds-type {session|assume-role|web-identity}
      --assume-role-target  Role ARN (AWS) / SA email (GCP) to assume/impersonate
      --web-identity-token-file  File holding the OIDC token (web-identity)
      --session-name / --duration-seconds  Optional assume-role tuning
    Secrets are read from the environment, never CLI args (session: AWS_*
    or MULTICLOUD_E2E_BEARER_TOKEN; web-identity: --web-identity-token-file).

    \b
    Examples:
      python-multicloud-sdk-e2e iam --provider aws --tenant-id 123456789012
      python-multicloud-sdk-e2e iam --provider gcp --tenant-id my-project --region us-central1
      python-multicloud-sdk-e2e iam --provider aws --tenant-id 123456789012 \\
        --creds-type assume-role --assume-role-target arn:aws:iam::123456789012:role/e2e
    """
    from multicloud_e2e.iam.example import run

    run(
        provider=provider,
        identity_name=identity_name,
        tenant_id=tenant_id,
        region=region,
        trusted_principal=trusted_principal,
        creds_type=creds_type,
        assume_role_target=assume_role_target,
        web_identity_token_file=web_identity_token_file,
        session_name=session_name,
        duration_seconds=duration_seconds,
        interactive=ctx.obj["interactive"],
        do_cleanup=ctx.obj["cleanup"],
        verbose=ctx.obj["verbose"],
    )
