# Feature Roadmap

Python Multi-Cloud SDK roadmap — targeting **dev-ready by July 2026**.

## Current Status

✅ = Available &nbsp; 🚧 = In Progress (interface defined, provider not implemented)

### Blob Storage (`BucketClient`)

| API | Description | AWS | GCP |
|-----|-------------|:---:|:---:|
| `upload` | Upload bytes or file-like object to a key | ✅ | ✅ |
| `upload_from_path` | Upload from a local file path | ✅ | ✅ |
| `download` | Download object content to memory | ✅ | ✅ |
| `download_to_path` | Download directly to a local file path | ✅ | ✅ |
| `download_to_file` | Download directly to a file-like object | ✅ | ✅ |
| `download_stream` | Streaming download (lazy chunk iteration) | ✅ | ✅ |
| `delete` | Delete a single object (idempotent) | ✅ | ✅ |
| `delete_objects` | Batch delete multiple objects | ✅ | ✅ |
| `copy` | Server-side copy to another bucket | ✅ | ✅ |
| `copy_from` | Server-side copy from another bucket | ✅ | ✅ |
| `list_page` | List blobs with prefix/delimiter/pagination | ✅ | ✅ |
| `does_object_exist` | Check if an object exists | ✅ | ✅ |
| `does_bucket_exist` | Check if the bucket exists | ✅ | ✅ |
| `get_tags` | Retrieve object tags | ✅ | ✅ |
| `set_tags` | Set/replace object tags | ✅ | ✅ |
| `generate_presigned_url` | Generate presigned URL for upload or download | ✅ | ✅ |
| `list_blob_versions` | List all versions of an object [^order] | ✅ | ✅ |
| `initiate_multipart_upload` | Start a multipart upload session | ✅ | ✅ |
| `upload_part` | Upload a single part | ✅ | ✅ |
| `complete_multipart_upload` | Assemble parts into the final object | ✅ | ✅ |
| `list_parts` | List uploaded parts for a multipart upload | ✅ | ✅ |
| `abort_multipart_upload` | Abort and discard uploaded parts | ✅ | ✅ |

[^order]: Version order is provider-native and not normalised, so it differs
between clouds. `is_latest` identifies the current version on every provider,
and `last_modified` is populated on every entry, so sort on it if order matters.

### STS (`StsClient`)

| API | Description | AWS | GCP |
|-----|-------------|:---:|:---:|
| `get_caller_identity` | Return identity info for current credentials | ✅ | ✅ |
| `assume_role` | Assume a role and return temporary credentials | ✅ | ✅ |
| `get_access_token` | Retrieve a fresh access token | ✅ | ✅ |
| `generate_service_account_token` | Generate token via service account impersonation | ✅ | ✅ |
| `assume_role_with_web_identity` | Exchange OIDC/JWT token for temporary credentials | ✅ | ✅ |

### IAM (`IamClient`)

| API | Description | AWS | GCP |
|-----|-------------|:---:|:---:|
| `create_identity` | Create an identity (idempotent) | ✅ | ✅ |
| `get_identity` | Retrieve identity identifier | ✅ | ✅ |
| `delete_identity` | Delete an identity | ✅ | ✅ |
| `attach_inline_policy` | Attach an inline policy to an identity | ✅ | ✅ |
| `get_inline_policy_details` | Retrieve an inline policy document | ✅ | ✅ |
| `get_attached_policies` | List policies attached to an identity | ✅ | ✅ |
| `remove_policy` | Remove a policy from an identity | ✅ | ✅ |

### PubSub (`TopicClient` / `SubscriptionClient`)

| API | Description | AWS | GCP |
|-----|-------------|:---:|:---:|
| `send` | Send a single message to a topic | 🚧 | 🚧 |
| `receive` | Receive a message from a subscription (blocking) | 🚧 | 🚧 |
| `send_ack` | Acknowledge a single message | 🚧 | 🚧 |
| `send_acks` | Batch acknowledge multiple messages | 🚧 | 🚧 |
| `send_nack` | Negatively acknowledge (requeue for redelivery) | 🚧 | 🚧 |
| `send_nacks` | Batch negative acknowledge | 🚧 | 🚧 |
| `can_nack` | Check if provider supports negative acknowledgment | 🚧 | 🚧 |
| `get_attributes` | Retrieve subscription attributes | 🚧 | 🚧 |
| `is_retryable` | Check if an error is transient/retryable | 🚧 | 🚧 |

## Milestones

| Target | Service | Milestone |
|--------|---------|-----------|
| July 2026 | Blob Storage | AWS + GCP dev-ready |
| July 2026 | STS | AWS + GCP dev-ready |
| July 2026 | PubSub | AWS + GCP dev-ready |
| July 2026 | IAM | AWS + GCP dev-ready |
