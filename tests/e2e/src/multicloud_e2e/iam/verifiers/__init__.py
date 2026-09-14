"""IAM verifier dispatch — returns the right verifier for a given provider."""

from multicloud_e2e.common.verifier import ServiceVerifier


def get_iam_verifier(provider: str, region: str = None) -> ServiceVerifier:
    """Get the IAM verifier for the specified provider."""
    if provider == "aws":
        from multicloud_e2e.iam.verifiers.aws import AwsIamVerifier

        return AwsIamVerifier(region=region)
    elif provider == "gcp":
        from multicloud_e2e.iam.verifiers.gcp import GcpIamVerifier

        return GcpIamVerifier(region=region)
    else:
        raise ValueError(f"No IAM verifier for provider: {provider}")
