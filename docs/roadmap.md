---
hide:
  - toc
---

# Feature Roadmap

Browse the SDK APIs by service. Support describes whether an operation is
implemented; provider-specific behavior is explained in the notes.

<div class="roadmap" markdown="1">

<nav class="roadmap-nav" aria-label="Jump to a service">
<a href="#blob-storage">Blob Storage</a>
<a href="#sts">STS</a>
<a href="#iam">IAM</a>
<a href="#pubsub">Pub/Sub</a>
<a href="#document-store">Document Store</a>
</nav>

<p class="support-legend"><span class="api-support api-support--yes" aria-hidden="true">✓</span> Supported <span class="api-support api-support--no" aria-hidden="true">—</span> Unsupported</p>

## Blob Storage { #blob-storage }

[Usage guide](guides/blobstore-guide.md) · [API reference](api/blob.md)

=== "Sync client"

    `BucketClient` supports every operation below on AWS and GCP.

    #### Upload, download, and copy

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `upload` | Upload bytes or a binary stream | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `upload_from_path` | Upload from a local file | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download` | Download object content | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_to_path` | Download to a local path | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_to_file` | Download to a binary file | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_stream` | Iterate content chunks | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `copy` | Copy to another bucket | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `copy_from` | Copy from another bucket | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `delete` | Delete one object | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `delete_objects` | Delete multiple objects | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Listing and metadata

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `list_page` | List with prefix, delimiter, and pagination | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `list_blob_versions` | List object versions | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `does_object_exist` | Check object existence | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `does_bucket_exist` | Check bucket existence | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `get_metadata` | Read object metadata | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `get_tags` | Read tags | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `set_tags` | Replace tags | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `generate_presigned_url` | Generate a signed upload or download URL | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Object protection

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `get_object_lock` | Read retention and legal hold | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `update_object_retention` | Update retention | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `update_legal_hold` | Update legal hold | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Multipart upload

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `initiate_multipart_upload` | Start a multipart upload | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `upload_part` | Upload a part | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `complete_multipart_upload` | Complete a multipart upload | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `list_parts` | List uploaded parts | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `abort_multipart_upload` | Abort a multipart upload | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Client lifecycle

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `close` | Release client resources | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    Use `with` for automatic client cleanup.

=== "Async client"

    `AsyncBucketClient` supports every operation below on AWS and GCP.

    #### Upload, download, and copy

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `upload` | Upload bytes or a binary stream | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `upload_from_path` | Upload from a local file | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download` | Download object content | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_to_path` | Download to a local path | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_to_file` | Download to a binary file | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_stream` | Iterate content chunks | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `copy` | Copy to another bucket | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `copy_from` | Copy from another bucket | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `delete` | Delete one object | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `delete_objects` | Delete multiple objects | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Listing and metadata

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `list_page` | List with prefix, delimiter, and pagination | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `list_blob_versions` | List object versions | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `does_object_exist` | Check object existence | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `does_bucket_exist` | Check bucket existence | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `get_metadata` | Read object metadata | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `get_tags` | Read tags | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `set_tags` | Replace tags | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `generate_presigned_url` | Generate a signed upload or download URL | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Object protection

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `get_object_lock` | Read retention and legal hold | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `update_object_retention` | Update retention | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `update_legal_hold` | Update legal hold | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Multipart upload

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `initiate_multipart_upload` | Start a multipart upload | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `upload_part` | Upload a part | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `complete_multipart_upload` | Complete a multipart upload | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `list_parts` | List uploaded parts | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `abort_multipart_upload` | Abort a multipart upload | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Directory operations

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `upload_directory` | Upload local directory contents | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `download_directory` | Download objects under a prefix | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
    | `delete_directory` | Delete objects under a prefix | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    #### Client lifecycle

    | API | Purpose | AWS | GCP |
    | --- | --- | :---: | :---: |
    | `close` | Release client resources | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

    Await coroutine operations and use `async with` for cleanup.
    `list_blob_versions()` returns an async iterator directly:

    ```python
    async for version in client.list_blob_versions("report.pdf"):
        print(version.version_id)
    ```

??? info "Blob Storage provider notes"

    Object protection needs appropriate resource configuration and permissions.
    AWS uses ETag write matching and GCP uses generation/version matching. A
    standalone unsupported token raises an error; when both `if_match` and
    `if_version_match` are supplied, each provider uses its supported token.
    Version ordering remains provider-native; use `is_latest` or sort explicitly.

## STS { #sts }

`StsClient` provides identity and temporary-credential operations.
[Usage guide](guides/sts-guide.md) · [API reference](api/sts.md)

| API | Purpose | AWS | GCP |
| --- | --- | :---: | :---: |
| `get_caller_identity` | Identify the caller | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `assume_role` | Obtain temporary role credentials | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `get_access_token` | Obtain refreshed or scoped credentials | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `generate_service_account_token` | Impersonate a service account | <span class="api-support api-support--no" title="Unsupported"><span aria-hidden="true">—</span><span class="support-label">Unsupported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `assume_role_with_web_identity` | Exchange a web-identity token | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `close` | Release client resources | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

`StsUtilities` exposes the following additional operation:

| API | Purpose | AWS | GCP |
| --- | --- | :---: | :---: |
| `create_signed_auth_request` | Create a signed caller-identity authentication request | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--no" title="Unsupported"><span aria-hidden="true">—</span><span class="support-label">Unsupported</span></span> |

??? info "STS provider notes"

    All operations marked supported have implementations. Credential types,
    permissions, and token parameters are prerequisites, not a separate support
    level. GCP identity/audience tokens need compatible service-account or
    impersonated credentials; scoped tokens need a service-account email.

    AWS `get_access_token` uses session tokens and ignores `audience` and
    `scopes`. GCP `assume_role` impersonates a service account and ignores
    `session_name`, `external_id`, `policy`, and `tags`; use the implemented
    `credential_scope` path for downscoping. Token lifetimes remain provider-specific.

    AWS `generate_service_account_token` and GCP
    `create_signed_auth_request` explicitly raise `UnsupportedOperationException`.
    These are the two unsupported provider paths shown above.

## IAM { #iam }

`IamClient` manages identities and permissions.
[Usage guide](guides/iam-guide.md) · [API reference](api/iam.md)

| API | Purpose | AWS | GCP |
| --- | --- | :---: | :---: |
| `create_identity` | Create or resolve an identity | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `get_identity` | Read its identifier | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `delete_identity` | Delete an identity | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `attach_inline_policy` | Attach policy permissions | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `get_inline_policy_details` | Read a policy document or binding | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `get_attached_policies` | List attached policy names or roles | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `remove_policy` | Remove policy permissions | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `close` | Release client resources | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

??? info "IAM provider notes"

    All listed IAM APIs are implemented. Their policy representations differ:
    AWS uses named inline policies; GCP uses project-level role bindings.
    On GCP, lookup/removal uses role names such as `roles/storage.objectViewer`.

    GCP applies ALLOW statements, skips DENY statements, rejects conditions on
    translated ALLOW statements, and does not use statement resources to restrict
    the binding. Read the IAM guide before applying a policy: the same method
    name does not guarantee equivalent policy semantics on every provider.

## Pub/Sub { #pubsub }

Publish with `TopicClient`; receive and acknowledge with `SubscriptionClient`.
[Usage guide](guides/pubsub-guide.md) · [API reference](api/pubsub.md)

| API | Purpose | AWS | GCP |
| --- | --- | :---: | :---: |
| `send` | Publish a message (TopicClient) | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `receive` | Receive a message (SubscriptionClient) | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `send_ack` | Acknowledge one message | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `send_acks` | Batch acknowledgments; returns a Future | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `send_nack` | Make one message available for redelivery | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `send_nacks` | Batch negative acknowledgments; returns a Future | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `can_nack` | Check negative-acknowledgment capability | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `get_attributes` | Read subscription metadata | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `is_retryable` | Classify a failure for retry | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `close` | Flush pending work and release the client | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

??? info "Pub/Sub provider notes"

    AWS publishing selects `awssns` or `awssqs`; AWS subscriptions use `aws`.
    GCP uses `gcp`. Both clients expose `close`; the other rows identify their
    relevant client. Batch acknowledgments return Futures whose results should
    be observed. Redelivery follows provider visibility/deadline limits.

    The current `get_attributes` implementations leave
    `approximate_number_of_messages` unset. `is_retryable` classifies errors;
    it does not promise to retry every failed operation.

## Document Store { #document-store }

`DocStoreClient` supports document operations and batching through `actions()`.
[API reference](api/docstore.md)

| API | Purpose | AWS | GCP |
| --- | --- | :---: | :---: |
| `actions` | Build a list of document actions | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `create` | Create a document | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `replace` | Replace an existing document | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `put` | Upsert a document | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `delete` | Delete a document | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `get` | Read into a supplied document | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `batch_get` | Read multiple documents | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `batch_put` | Upsert multiple documents | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `batch_delete` | Delete multiple documents | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |
| `close` | Release client resources | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> | <span class="api-support api-support--yes" title="Supported"><span aria-hidden="true">✓</span><span class="support-label">Supported</span></span> |

??? info "Batching within DocStoreClient"

    `actions()` returns a fluent builder within the DocStore API. Chain
    `create`, `replace`, `put`, `delete`, or `get` and execute with `run()`.
    `enable_atomic_writes()` requests transactional writes, and `before_do()`
    registers a before-operation hook. These belong to DocStore batching,
    rather than a separate service interface.

    Revision checks, supported field shapes, and transaction limits depend on
    the provider. Query/filter/order/pagination APIs are not exposed by the
    current public `DocStoreClient`.

</div>

Ali provider distributions for Blob Storage, STS, and Pub/Sub remain placeholders.
The tables show the implemented AWS and GCP providers. Reviewed October 22, 2026.
