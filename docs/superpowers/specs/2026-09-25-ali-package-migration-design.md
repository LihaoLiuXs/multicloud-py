# Ali Package Naming Alignment Design

## Status

Approved in conversation on 2026-09-25. This document records the design before implementation.

## Context

MultiCloudJ uses `ali` consistently for the Ali provider: Maven modules such as `blob-ali` and `sts-ali`, Java namespaces ending in `.ali`, and the runtime provider ID `"ali"`.

The Python SDK currently uses a different, longer provider label in three distribution names and their provider entry points. These provider packages are placeholders whose implementations are still pending, and the project has no users relying on the published names.

The old production PyPI projects already exist:

| Project | Published versions |
| --- | --- |
| Legacy Blob provider project | `0.1.6`, `0.1.7`, `0.1.8` |
| Legacy Pub/Sub provider project | `0.1.6`, `0.1.7`, `0.1.8` |
| Legacy STS provider project | `0.1.7`, `0.1.8` |

PyPI project identities cannot be renamed in place. The three new `*-ali` names are currently unclaimed, so the migration must create new PyPI projects.

## Goals

- Align the Python SDK's owned technical identifiers with MultiCloudJ's `ali` convention.
- Publish exactly 21 Python distributions after the migration, replacing the three old names with three new names.
- Use `ali` terminology consistently in identifiers and human-readable repository content.
- Remove every case-insensitive occurrence of the retired provider token from tracked paths and file content.
- Keep the automated release process simple: merging the Release Please PR validates and publishes all 21 distributions from `release.yml`.
- Avoid compatibility machinery because there are no consumers of the old placeholder distributions.

## Non-goals

- Publishing compatibility or redirect packages under the old names.
- Deleting the old PyPI projects.
- Changing provider behavior or implementing the placeholder providers.

## Naming Rules

All repository naming will use `ali`:

| Area | Required value |
| --- | --- |
| Blob distribution | `multicloud-py-blob-ali` |
| Pub/Sub distribution | `multicloud-py-pubsub-ali` |
| STS distribution | `multicloud-py-sts-ali` |
| Package directories | `multicloud-*-ali` |
| Provider entry-point name | `ali` |
| Runtime provider ID | `"ali"` |
| Repository-owned symbols | `ali_*` / `ALI_*` where applicable |
| Documentation examples | `provider="ali"` |

Python import namespaces already use `blob_ali`, `pubsub_ali`, and `sts_ali`; they remain unchanged.

Descriptive prose will use “Ali provider,” “Ali OSS,” “Ali MNS,” and “Ali RAM.” Repository-owned credential-variable names will also use the `ALI_*` prefix. If a future provider SDK requires an external identifier with the retired spelling, that exception requires separate review rather than being introduced by this migration.

## Repository Changes

The implementation will:

1. Rename the three provider package directories.
2. Update their `project.name` metadata and workspace keys.
3. Change provider entry-point keys and repository-owned provider IDs from the retired label to `ali`.
4. Update client documentation, examples, tests, fixtures, credential-variable names, and symbols to use `ali`.
5. Replace the three old distribution names in the production and TestPyPI workflow matrices.
6. Update release workflow tests and artifact validation expectations so the canonical set remains exactly 21 distributions.
7. Regenerate `uv.lock` from the renamed workspace members.
8. Update maintainer release documentation with the new publisher registrations and retirement treatment for the old projects.

A final case-insensitive search for the retired provider token must return no tracked paths or file-content matches.

## PyPI and TestPyPI Migration

### Production PyPI

Create pending trusted publishers for:

| Project | Workflow | Environment |
| --- | --- | --- |
| `multicloud-py-blob-ali` | `release.yml` | `pypi-multicloud-py-blob-ali` |
| `multicloud-py-pubsub-ali` | `release.yml` | `pypi-multicloud-py-pubsub-ali` |
| `multicloud-py-sts-ali` | `release.yml` | `pypi-multicloud-py-sts-ali` |

Every publisher uses GitHub owner `salesforce` and repository `multicloud-py`.

The other 18 existing production projects must trust `release.yml` before the release PR is merged. The three obsolete legacy projects do not need a new `release.yml` publisher because the release workflow will no longer publish them.

### TestPyPI

Create pending trusted publishers for the same three new project names using workflow `test-pypi.yml` and environments:

- `testpypi-multicloud-py-blob-ali`
- `testpypi-multicloud-py-pubsub-ali`
- `testpypi-multicloud-py-sts-ali`

The old TestPyPI projects remain owned but are no longer included in the test-publishing matrix.

### Old Projects

The old production and TestPyPI projects remain under Salesforce ownership and frozen at their existing versions. They will not be deleted, because retaining ownership prevents another party from reusing potentially confusing names. Yanking their releases with a reason pointing to the corresponding `*-ali` package is an optional, separately reviewed operational step; it is not required for this migration.

## Release Sequencing

1. Implement and validate the repository rename on `main`.
2. Let Release Please update PR #10 to include the rename in release `0.1.9`.
3. Register the three new TestPyPI pending publishers.
4. Run the relevant TestPyPI batches and verify that the three new projects are created and contain valid artifacts.
5. Register the three new production pending publishers for `release.yml`.
6. Verify that all 18 unchanged production projects also trust `release.yml`.
7. Keep PR #10 unmerged until the production publisher audit is complete.
8. Merge PR #10. The same `release.yml` run creates the GitHub release, validates all 21 distributions, and publishes them to production PyPI.
9. Verify the GitHub release and all 21 PyPI project versions.

## Validation

The change is complete only after all of the following pass:

- Release-workflow configuration tests, including an exact 21-project matrix.
- `uv lock --check` and a frozen all-package synchronization.
- Formatting and import-order checks.
- Full non-conformance test suite.
- Replay-mode conformance test suite.
- Build of exactly 21 wheels and 21 source distributions.
- Twine metadata validation for every artifact.
- Repository artifact metadata/license validation.
- Isolated installation and import smoke tests for every wheel.
- Targeted tests proving `ali` is the discoverable provider entry-point name.
- A case-insensitive repository search showing no remaining path or content containing the retired provider token.
- Successful TestPyPI publication and installation of all three new `*-ali` distributions.
- After PR #10 is merged, successful production publication of all 21 distributions.

## Risks and Mitigations

### Old package names remain installable

The old projects cannot be transparently renamed. They remain at `0.1.8`, but there are no known consumers and the packages are implementation-pending placeholders. Salesforce retains ownership to prevent name reuse. Optional yanking can make accidental new installations less likely.

### Trusted Publisher mismatch

PyPI matches the repository, workflow filename, and GitHub environment exactly. The release PR must remain unmerged until the three new projects and the 18 retained projects trust `release.yml` with their exact `pypi-<project>` environment names.

### Partial rename

Tests will assert the exact distribution set and provider entry-point names. A final case-insensitive search will catch the retired token in both tracked paths and file content; no exceptions are allowed in this migration.

### Release contains a new project set

TestPyPI publication precedes production. Artifact validation and isolated installation checks run before any production publish job, and the release workflow retains a single release-wide concurrency group.
