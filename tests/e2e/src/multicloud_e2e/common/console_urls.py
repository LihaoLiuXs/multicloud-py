"""Console URL builders keyed by provider and resource type."""

from typing import Any


def console_url(provider: str, resource_type: str, **kwargs: Any) -> str:
    """Build a clickable cloud console URL for a given resource."""
    if provider == "aws":
        if resource_type == "iam_identity":
            return f"https://us-east-1.console.aws.amazon.com/iam/home#/roles/details/{kwargs['identity_name']}"
        if resource_type == "iam_identity_list":
            return "https://us-east-1.console.aws.amazon.com/iam/home#/roles"
        if resource_type == "iam_policy":
            return f"https://us-east-1.console.aws.amazon.com/iam/home#/roles/details/{kwargs['identity_name']}?section=permissions"
    elif provider == "gcp":
        tenant_id = kwargs.get("tenant_id", "")
        if resource_type == "iam_identity":
            return f"https://console.cloud.google.com/iam-admin/serviceaccounts?project={tenant_id}"
        if resource_type == "iam_identity_list":
            return f"https://console.cloud.google.com/iam-admin/serviceaccounts?project={tenant_id}"
        if resource_type == "iam_policy":
            identity_name = kwargs.get("identity_name", "")
            email = (
                identity_name
                if "@" in identity_name
                else f"{identity_name}@{tenant_id}.iam.gserviceaccount.com"
            )
            return f"https://console.cloud.google.com/iam-admin/iam?project={tenant_id}&principalEmail={email}"
    return ""
