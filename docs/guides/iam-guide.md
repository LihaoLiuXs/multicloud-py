# IAM guide

`IamClient` manages identities and their permissions through a shared interface.
AWS uses IAM roles; GCP uses service accounts with project-level role bindings.
All seven public IAM operations are implemented for AWS and GCP, with the
policy differences below. There is no implemented Ali IAM provider.

## Install and configure

```bash
pip install multicloud-py-iam-aws
# Or select the GCP provider:
pip install multicloud-py-iam-gcp
```

Configure the selected provider's normal credential chain before constructing
clients. `tenant_id` is an AWS account ID or GCP project ID. The caller needs
permissions for the identity and policy operations it will perform. Use an
isolated test account/project for the mutating examples.

## Supported operations

| Operation | Purpose |
| --- | --- |
| `create_identity` | Create an identity or resolve an existing one |
| `get_identity` | Return its provider identifier |
| `delete_identity` | Delete the identity |
| `attach_inline_policy` | Apply a `PolicyDocument` |
| `get_inline_policy_details` | Return policy JSON or binding details, or `None` |
| `get_attached_policies` | List policy names or role names |
| `remove_policy` | Remove the named policy or role binding |

## Read an existing identity

Replace these environment values with an identity and account/project you can
access. `REGION` is optional and interpreted by the provider.

```python
import os
from multicloud.iam.iam_client import IamClient

with IamClient(
    provider=os.environ["PROVIDER"],
    region=os.environ.get("REGION"),
) as client:
    identity_id = client.get_identity(
        os.environ["IDENTITY_NAME"], tenant_id=os.environ["TENANT_ID"]
    )
    print(identity_id)
    print(client.get_attached_policies(
        os.environ["IDENTITY_NAME"], tenant_id=os.environ["TENANT_ID"]
    ))
```

## Create and clean up a test identity

This example creates a uniquely named identity, reads it back, and deletes it.
Creation can have additional side effects: resolving an existing AWS role can
also update its configuration. Use a fresh name, not an existing production
identity. IAM changes may take time to propagate.

```python
import os
from uuid import uuid4
from multicloud.iam.iam_client import IamClient

name = "docs-demo-" + uuid4().hex[:12]
tenant_id = os.environ["TENANT_ID"]
with IamClient(
    provider=os.environ["PROVIDER"], region=os.environ.get("REGION")
) as client:
    identity_id = client.create_identity(
        name, tenant_id=tenant_id, description="Temporary documentation example"
    )
    try:
        print(identity_id)
        print(client.get_identity(name, tenant_id=tenant_id))
    finally:
        client.delete_identity(name, tenant_id=tenant_id)
```

`TrustConfiguration` describes trusted principals. `CreateOptions` provides
options such as role path, session duration, and permission boundary where
implemented; these options are not portable restrictions on GCP service
accounts. See the [API reference](../api/iam.md) for exact types and signatures.

## Attach and inspect permissions

Construct a policy using the SDK's action and statement types:

```python
from multicloud.iam.iam_client import (
    Effect, PolicyDocument, Statement, StorageActions,
)

policy = PolicyDocument(
    name="ReadExampleObjects",
    statements=(Statement(
        effect=Effect.ALLOW,
        actions=(StorageActions.GET_OBJECT,),
        resources=("storage://example-bucket/*",),
    ),),
)
```

For an authorized identity, pass it to
`client.attach_inline_policy(name, policy_document=policy, tenant_id=tenant_id)`.
**Review the provider mapping before applying a policy**:

| Behavior | AWS | GCP |
| --- | --- | --- |
| Identity identifier | Role ARN | Service-account email |
| Policy attachment | Named role inline policy | Project IAM role bindings |
| Lookup/removal name | `PolicyDocument.name` | A returned role name, e.g. `roles/storage.objectViewer` |
| ALLOW statements | Translated to a native policy | Known actions map to predefined roles |
| DENY statements | Translated to a native policy | Skipped by the attachment implementation |
| Conditions | Translated to native conditions | Rejected for translated ALLOW statements |
| Statement resources | Translated by the AWS policy translator | Do not restrict the project-level role binding |

The sample `storage:GetObject` action maps to the predefined object-viewer
role on GCP. Its `resources` entry does **not** limit that grant to the example
bucket. Predefined roles can grant more permissions than one SDK action;
inspect the mapped role and intended project before attaching it. This API
is not a portable least-privilege policy compiler.

Use `get_attached_policies` to discover the names to pass to
`get_inline_policy_details` and `remove_policy`. Before deleting a test identity,
remove permissions attached during the test. GCP bindings live on the project;
deleting the service account alone does not remove those bindings.

## Errors and cleanup

Catch cloud-agnostic SDK exceptions such as `InvalidArgumentException`,
`ResourceNotFoundException`, and `UnAuthorizedException` from
`multicloud.common.exceptions`. Missing parameters fail validation; native
permission failures are translated by the provider. Methods do not promise
that all provider-native policy structures have an equivalent SDK shape.

Use a context manager to release client resources and explicit cleanup for
identities and bindings created by your application. The context manager
closes clients; it does not delete cloud resources.
