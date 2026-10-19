# Releasing MultiCloud Python

All 21 `multicloud-py-*` distributions share the version in `VERSION` and
are released together under one `multicloud-py-v*` tag.

## How the release version is chosen

We use [Release Please](https://github.com/googleapis/release-please) to open
and maintain a release pull request after changes are merged to `main`. It is
the release automation project maintained by the
[googleapis](https://github.com/googleapis) GitHub organization.

The version-increment policy is provided by Release Please's built-in
[`DefaultVersioningStrategy`](https://github.com/googleapis/release-please/blob/main/src/versioning-strategies/default.ts),
not by custom Python SDK code. Our
[`release-please-config.json`](https://github.com/salesforce/multicloud-py/blob/main/release-please-config.json)
selects the `simple` file-update strategy, specifies the `multicloud-py` tag
component, and tells Release Please to update `VERSION`; it does **not**
override the version-increment policy.

The version line in `VERSION` carries Release Please's required
`x-release-please-version` annotation. Repository scripts parse only the
semantic version before that annotation, while Release Please uses the marker
to update the file in every generated release PR.

| Change merged since the last release | Commit convention | Result from `0.1.4` |
| --- | --- | --- |
| Backward-compatible bug fix | `fix: ...` | `0.1.5` |
| Backward-compatible capability | `feat: ...` | `0.2.0` |
| Breaking public API change | `feat!: ...` or a `BREAKING CHANGE:` footer | `1.0.0` |

Release Please calculates the largest required increment among the commits
since the previous release. It records the last released version in
[`.release-please-manifest.json`](https://github.com/salesforce/multicloud-py/blob/main/.release-please-manifest.json),
then proposes the next version in a release PR. The release configuration uses
`include-component-in-tag` and `include-v-in-tag`, so version `0.1.4` produces
the Git tag `multicloud-py-v0.1.4`.

The same default applies before `1.0.0`: `0.9.9` becomes `0.9.10` for a patch,
`0.10.0` for a feature, and `1.0.0` for a breaking change. Reaching `0.9.9`
does not itself trigger a major release.

Use `Release-As: X.Y.Z` in a commit footer only when intentionally overriding
the calculated version, such as the initial `0.1.4` bootstrap release. Do not
manually edit the manifest for an ordinary release.

After Release Please updates `VERSION`, the
[`sync-release-pr` job](https://github.com/salesforce/multicloud-py/blob/main/.github/workflows/release.yml)
runs `scripts/sync_version.py` and `uv lock` on the generated release branch.
This propagates the selected version to every distribution's `pyproject.toml`,
inter-package dependency constraints, and lock file before the release PR is
reviewed.

## One-time administrator setup

1. In GitHub Actions settings, allow workflows to create pull requests and
   grant read/write workflow permissions. Release Please and the synchronization
   job use the repository `GITHUB_TOKEN`; do not add a personal access token or
   GitHub App credential.
2. Create one protected GitHub environment per distribution, with the required
   release reviewers. The publishing workflows use
   `testpypi-<distribution-name>` for TestPyPI and
   `pypi-<distribution-name>` for production PyPI.
   Each production PyPI project must trust workflow `release.yml` with its
   matching `pypi-<distribution-name>` environment.
3. TestPyPI permits only three pending Trusted Publishers at a time. Register
   the three distributions in one bootstrap batch with owner `salesforce`,
   repository `multicloud-py`, workflow `test-pypi.yml`, and environment
   `testpypi-<distribution-name>`. Run **TestPyPI Rehearsal** for that batch;
   successful publication converts those three entries to active publishers and
   frees three pending slots. Repeat for all seven batches before the complete
   TestPyPI installation rehearsal.
4. Require the normal pull-request CI workflow on `main`.

## Normal release process

The maintainer-facing process intentionally matches MultiCloudJ:

1. Merge conventional commits to `main`.
2. Release Please opens or updates one release PR. The synchronization job
   updates all 21 package manifests and `uv.lock` on that branch.
3. Review the release PR and ensure its normal CI checks pass.
4. Merge the release PR. No separate publishing workflow needs to be started.
5. The same `release.yml` run creates the Git tag and GitHub release, builds 21
   wheels and 21 sdists, runs the unit and replay-mode conformance suites,
   validates metadata and licenses, and installs each wheel independently.
6. Only after validation succeeds, the workflow publishes the exact validated
   artifacts through the 21 `pypi-<distribution-name>` environments. The
   release run remains in progress until every PyPI project finishes, and it
   fails if any publication fails.

The `release-main` concurrency group serializes the complete process, including
publication. PyPI and TestPyPI versions are immutable. The publisher uses
PyPI's duplicate-skipping support so a failed matrix job can be rerun without
re-uploading an artifact that already succeeded. Rerun only failed jobs; never
delete or overwrite published artifacts.

## Migrating the production Trusted Publishers to `release.yml`

The retained production projects were initially created with
`bootstrap-pypi.yml`. Before merging the next release PR, update each of the 18
retained PyPI projects under **Manage → Publishing**:

- Owner: `salesforce`
- Repository: `multicloud-py`
- Workflow: `release.yml`
- Environment: `pypi-<distribution-name>`

Add the `release.yml` publisher first, then remove the old
`bootstrap-pypi.yml` publisher.

The three new Ali distributions require these exact registrations:

| Distribution | Production workflow | Production environment | Test workflow | Test environment |
| --- | --- | --- | --- | --- |
| `multicloud-py-blob-ali` | `release.yml` | `pypi-multicloud-py-blob-ali` | `test-pypi.yml` | `testpypi-multicloud-py-blob-ali` |
| `multicloud-py-pubsub-ali` | `release.yml` | `pypi-multicloud-py-pubsub-ali` | `test-pypi.yml` | `testpypi-multicloud-py-pubsub-ali` |
| `multicloud-py-sts-ali` | `release.yml` | `pypi-multicloud-py-sts-ali` | `test-pypi.yml` | `testpypi-multicloud-py-sts-ali` |

Keep release PR #10 unmerged until these three new production publishers and
all 18 retained production publishers trust `release.yml`. Existing TestPyPI
publishers continue using `test-pypi.yml`; only the three new Ali projects need
new TestPyPI registrations.
