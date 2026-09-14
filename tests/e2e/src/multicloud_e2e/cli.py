"""Click CLI entry point for the E2E testing harness.

Usage:
    python-multicloud-sdk-e2e iam --provider aws --tenant-id 123456789012
    python-multicloud-sdk-e2e iam --provider gcp --tenant-id my-project
    python-multicloud-sdk-e2e --no-pause iam --provider gcp --tenant-id my-project
"""

from __future__ import annotations

import click


@click.group()
@click.option("--no-pause", is_flag=True, help="Skip interactive pauses (CI-friendly).")
@click.option(
    "--no-cleanup", is_flag=True, help="Leave resources for manual inspection."
)
@click.option("--verbose", is_flag=True, help="Print full SDK responses.")
@click.pass_context
def cli(ctx: click.Context, no_pause: bool, no_cleanup: bool, verbose: bool) -> None:
    """python-multicloud-sdk-e2e — provider-agnostic E2E testing.

    Run end-to-end lifecycle tests against real cloud providers.
    Each subcommand tests a specific service interface.
    The same code works with any provider — only --provider changes.
    """
    ctx.ensure_object(dict)
    ctx.obj["interactive"] = not no_pause
    ctx.obj["cleanup"] = not no_cleanup
    ctx.obj["verbose"] = verbose


# Register service subcommands
from multicloud_e2e.iam.commands import iam  # noqa: E402

cli.add_command(iam)


if __name__ == "__main__":
    cli()
