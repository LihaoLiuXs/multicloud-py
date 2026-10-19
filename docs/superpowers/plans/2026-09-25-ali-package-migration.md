# Ali Package Naming Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align every repository-owned provider name with MultiCloudJ's `ali` convention and release the three renamed distributions through the existing 21-package automation.

**Architecture:** Perform one atomic repository rename guarded by a test that scans both tracked paths and tracked text for the retired provider token. Keep the release set at 21 by replacing three distribution identities, then rehearse the three new identities on TestPyPI before allowing Release Please PR #10 to publish them to production PyPI.

**Tech Stack:** Python 3.10+, uv workspace, setuptools, pytest, PyYAML, GitHub Actions, Release Please, PyPI/TestPyPI Trusted Publishing.

**Spec:** `docs/superpowers/specs/2026-09-25-ali-package-migration-design.md`

## Global Constraints

- All repository naming uses `ali`; the retired provider token must have zero case-insensitive matches in tracked paths and file content.
- Distribution names are exactly `multicloud-py-blob-ali`, `multicloud-py-pubsub-ali`, and `multicloud-py-sts-ali`.
- Runtime provider and entry-point IDs are exactly `"ali"`.
- The release set remains exactly 21 distributions.
- Do not add compatibility or redirect distributions.
- Do not delete the three legacy PyPI or TestPyPI projects.
- Do not implement or otherwise change provider behavior.
- Keep Release Please PR #10 unmerged until all 21 production projects trust `release.yml` under their exact `pypi-<distribution-name>` environments.
- Production publication requires explicit user confirmation after the publisher audit.
- Agent execution stops after Task 4. Tasks 5 and 6 are user-owned registration and release checklists; the agent must not create or change PyPI/TestPyPI registrations.

## Review Focus

- A retired token left in either a tracked path or text must fail a deterministic regression test, including uppercase credential variables and title-case prose.
- All three renamed distributions must appear exactly once in the 21-project production and TestPyPI matrices, with no legacy distribution identity remaining.
- All four Ali entry-point groups (Blob, Pub/Sub topic, Pub/Sub subscription, and STS) must expose only the key `ali`.
- `uv.lock` must resolve the three renamed workspace packages without stale workspace identities or broken intra-workspace dependencies.
- Re-running mixed TestPyPI batches must skip immutable artifacts already uploaded while still creating and validating each new Ali project.

---

## File Structure

### New file

- `tests/scripts/test_ali_naming.py`: owns the zero-retired-token invariant and validates the three distribution names and four entry-point keys.

### Renamed package roots

- `packages/blob/multicloud-blob-ali/`: Blob provider distribution.
- `packages/pubsub/multicloud-pubsub-ali/`: Pub/Sub provider distribution.
- `packages/sts/multicloud-sts-ali/`: STS provider distribution.

Each target is a Git rename of the corresponding existing provider directory whose suffix is the retired token assembled in the test as `"ali" + "baba"`.

### Packaging and release files

- `pyproject.toml`: workspace source keys and distribution inventory.
- `uv.lock`: generated lock data for the renamed workspace identities.
- `.github/workflows/release.yml`: production 21-project publish matrix.
- `.github/workflows/test-pypi.yml`: TestPyPI batches and idempotent re-publishing behavior.
- `tests/scripts/test_release_workflows.py`: exact release matrices, environments, and duplicate-skipping contract.
- `docs/maintainers/releasing.md`: operator checklist for the three new projects and the 18 retained projects.

### Public metadata and provider discovery

- `packages/blob/multicloud-blob-ali/pyproject.toml`: Blob distribution metadata and provider entry point.
- `packages/pubsub/multicloud-pubsub-ali/pyproject.toml`: Pub/Sub metadata and topic/subscription entry points.
- `packages/sts/multicloud-sts-ali/pyproject.toml`: STS distribution metadata and provider entry point.
- `packages/common/multicloud-common/multicloud/common/provider.py`: provider-discovery example.
- `packages/common/multicloud-common/multicloud/common/providers.py`: provider-discovery documentation.
- `packages/blob/multicloud-blob/**`: Blob public documentation and metadata references.
- `packages/pubsub/multicloud-pubsub/**`: Pub/Sub public metadata references.
- `packages/sts/multicloud-sts/**`: STS public documentation and metadata references.

### Repository documentation and test support

- `README.md`, `examples/README.md`, `mkdocs.yml`: top-level names, examples, and source roots.
- `docs/index.md`, `docs/design/layers.md`, `docs/guides/blobstore-guide.md`, `docs/guides/pubsub-guide.md`: provider prose and examples.
- `tests/conftest.py`: `skip_ali_tests`, `ali_credentials`, and `ALI_*` credential variables.
- `tests/blob/conformance/vcr_filters.py`, `tests/iam/conformance/vcr_filters.py`, `tests/sts/conformance/vcr_filters.py`: Ali terminology in filter documentation.

---

### Task 1: Add a Failing Repository Naming Contract

**Files:**
- Create: `tests/scripts/test_ali_naming.py`

**Interfaces:**
- Consumes: tracked files reported by `git ls-files`; TOML metadata in the three target provider packages after their rename.
- Produces: `_tracked_paths() -> list[Path]`, `test_retired_provider_token_is_absent()`, and `test_ali_distribution_names_and_entry_points()`.

- [ ] **Step 1: Write the failing naming tests**

```python
"""Regression coverage for the repository-wide Ali naming contract."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RETIRED_PROVIDER_TOKEN = "ali" + "baba"

ALI_PACKAGES = {
    Path("packages/blob/multicloud-blob-ali/pyproject.toml"): (
        "multicloud-py-blob-ali",
        ("multicloud.blob.providers",),
    ),
    Path("packages/pubsub/multicloud-pubsub-ali/pyproject.toml"): (
        "multicloud-py-pubsub-ali",
        (
            "multicloud.pubsub.topic.providers",
            "multicloud.pubsub.subscription.providers",
        ),
    ),
    Path("packages/sts/multicloud-sts-ali/pyproject.toml"): (
        "multicloud-py-sts-ali",
        ("multicloud.sts.providers",),
    ),
}


def _tracked_paths() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT
    )
    return [Path(item.decode()) for item in output.split(b"\0") if item]


def test_retired_provider_token_is_absent() -> None:
    token = RETIRED_PROVIDER_TOKEN.encode()
    violations: list[str] = []

    for relative_path in _tracked_paths():
        if RETIRED_PROVIDER_TOKEN in relative_path.as_posix().lower():
            violations.append(f"path:{relative_path}")

        content = (REPO_ROOT / relative_path).read_bytes()
        if b"\0" not in content and token in content.lower():
            violations.append(f"content:{relative_path}")

    assert violations == []


def test_ali_distribution_names_and_entry_points() -> None:
    for relative_path, (expected_name, groups) in ALI_PACKAGES.items():
        metadata = tomllib.loads((REPO_ROOT / relative_path).read_text())
        assert metadata["project"]["name"] == expected_name

        entry_points = metadata["project"]["entry-points"]
        for group in groups:
            assert set(entry_points[group]) == {"ali"}
```

- [ ] **Step 2: Run the tests to verify they fail for both paths and content**

Run:

```bash
uv run --frozen pytest tests/scripts/test_ali_naming.py -v
```

Expected: both tests fail. The first reports tracked path/content violations, and the second reports that the three target manifests do not exist yet.

- [ ] **Step 3: Confirm the test itself does not contain the retired token literally**

Run:

```bash
retired=$(printf '%s%s' ali baba)
rg -ni "$retired" tests/scripts/test_ali_naming.py
```

Expected: exit code 1 with no output.

---

### Task 2: Perform the Atomic Repository Rename

**Files:**
- Rename: the three existing retired-suffix package roots to the exact `*-ali` roots listed under File Structure.
- Modify: every tracked text file returned by the case-insensitive retired-token search.
- Modify: `pyproject.toml`, `uv.lock`, `.github/workflows/release.yml`, `.github/workflows/test-pypi.yml`.
- Test: `tests/scripts/test_ali_naming.py`, `tests/scripts/test_release_workflows.py`.

**Interfaces:**
- Consumes: `RETIRED_PROVIDER_TOKEN` naming invariant from Task 1.
- Produces: three `*-ali` distributions, provider entry-point key `ali`, runtime provider ID `ali`, and exact 21-project release matrices.

- [ ] **Step 1: Rename the three package roots with Git history preserved**

```bash
retired=$(printf '%s%s' ali baba)
git mv "packages/blob/multicloud-blob-${retired}" packages/blob/multicloud-blob-ali
git mv "packages/pubsub/multicloud-pubsub-${retired}" packages/pubsub/multicloud-pubsub-ali
git mv "packages/sts/multicloud-sts-${retired}" packages/sts/multicloud-sts-ali
```

- [ ] **Step 2: Replace all three letter-case forms in tracked text**

Apply a mechanical tracked-file rewrite:

```bash
retired_lower=$(printf '%s%s' ali baba)
retired_title=$(printf '%s%s' Ali baba)
retired_upper=$(printf '%s%s' ALI BABA)
git grep -Il -z -i "$retired_lower" \
  | xargs -0 perl -pi -e \
    "s/$retired_lower/ali/g; s/$retired_title/Ali/g; s/$retired_upper/ALI/g"
```

This changes distribution names, entry-point keys, provider examples, prose, test fixtures, and credential variables. Do not rewrite `.git`, `.venv`, caches, build output, or external package artifacts.

- [ ] **Step 3: Verify the three provider manifests expose the required metadata**

The resulting manifest fragments must be:

```toml
# packages/blob/multicloud-blob-ali/pyproject.toml
[project]
name = "multicloud-py-blob-ali"
description = "Ali OSS provider for multicloud-blob (placeholder; implementation pending)."

[project.entry-points."multicloud.blob.providers"]
ali = "multicloud.blob.blob_ali"
```

```toml
# packages/pubsub/multicloud-pubsub-ali/pyproject.toml
[project]
name = "multicloud-py-pubsub-ali"
description = "Ali MNS provider for multicloud-pubsub (placeholder; implementation pending)."

[project.entry-points."multicloud.pubsub.topic.providers"]
ali = "multicloud.pubsub.pubsub_ali"

[project.entry-points."multicloud.pubsub.subscription.providers"]
ali = "multicloud.pubsub.pubsub_ali"
```

```toml
# packages/sts/multicloud-sts-ali/pyproject.toml
[project]
name = "multicloud-py-sts-ali"
description = "Ali RAM provider for multicloud-sts (placeholder; implementation pending)."

[project.entry-points."multicloud.sts.providers"]
ali = "multicloud.sts.sts_ali"
```

- [ ] **Step 4: Make TestPyPI batch reruns idempotent**

Add the following input to the existing publish action in `.github/workflows/test-pypi.yml`:

```yaml
with:
  packages-dir: package-dist/
  repository-url: https://test.pypi.org/legacy/
  skip-existing: true
  verbose: true
```

This permits batches 3, 6, and 7 to create their new Ali project while skipping the two immutable distributions already uploaded in each batch.

- [ ] **Step 5: Strengthen the release workflow regression tests**

Add these assertions to `tests/scripts/test_release_workflows.py`:

```python
ALI_PROJECTS = {
    "multicloud-py-blob-ali",
    "multicloud-py-pubsub-ali",
    "multicloud-py-sts-ali",
}
```

In both matrix-coverage tests, assert:

```python
assert ALI_PROJECTS <= set(selected_projects)
```

In `test_testpypi_rehearsal_bootstraps_three_projects_per_selected_batch`, assert:

```python
upload = publish["steps"][-1]
assert upload["with"]["skip-existing"] is True
```

- [ ] **Step 6: Regenerate and validate the workspace lock**

Run:

```bash
uv lock
uv lock --check
uv sync --frozen --all-groups --all-packages
```

Expected: all commands succeed and `uv.lock` contains the three new distribution identities.

- [ ] **Step 7: Run the focused red-to-green suite**

Run:

```bash
uv run --frozen pytest tests/scripts/test_ali_naming.py tests/scripts/test_release_workflows.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Prove the repository has no retired path or content match**

Run:

```bash
retired=$(printf '%s%s' ali baba)
test -z "$(git ls-files | rg -i "$retired")"
test -z "$(git grep -Il -i "$retired")"
```

Expected: both checks succeed with no output.

- [ ] **Step 9: Commit the atomic rename**

```bash
git add -A
git commit -m "refactor: align ali provider naming"
```

---

### Task 3: Document and Test the Publisher Migration Contract

**Files:**
- Modify: `docs/maintainers/releasing.md`.
- Modify: `tests/scripts/test_release_workflows.py`.

**Interfaces:**
- Consumes: `ALI_PROJECTS` and the two release workflow filenames.
- Produces: an operator-visible mapping from each new distribution to its production and TestPyPI environment.

- [ ] **Step 1: Write the failing documentation contract test**

Add:

```python
def test_release_guide_lists_ali_trusted_publishers() -> None:
    guide = (REPO_ROOT / "docs" / "maintainers" / "releasing.md").read_text()

    for project in ALI_PROJECTS:
        expected_row = (
            f"| `{project}` | `release.yml` | `pypi-{project}` | "
            f"`test-pypi.yml` | `testpypi-{project}` |"
        )
        assert expected_row in guide
```

- [ ] **Step 2: Run the test and verify the missing table fails**

Run:

```bash
uv run --frozen pytest tests/scripts/test_release_workflows.py::test_release_guide_lists_ali_trusted_publishers -v
```

Expected: FAIL because the guide does not yet contain the exact rows.

- [ ] **Step 3: Add the exact publisher table and release gate**

Add this table to `docs/maintainers/releasing.md`:

```markdown
| Distribution | Production workflow | Production environment | Test workflow | Test environment |
| --- | --- | --- | --- | --- |
| `multicloud-py-blob-ali` | `release.yml` | `pypi-multicloud-py-blob-ali` | `test-pypi.yml` | `testpypi-multicloud-py-blob-ali` |
| `multicloud-py-pubsub-ali` | `release.yml` | `pypi-multicloud-py-pubsub-ali` | `test-pypi.yml` | `testpypi-multicloud-py-pubsub-ali` |
| `multicloud-py-sts-ali` | `release.yml` | `pypi-multicloud-py-sts-ali` | `test-pypi.yml` | `testpypi-multicloud-py-sts-ali` |
```

State immediately below it that PR #10 remains unmerged until the three new production publishers and the other 18 retained publishers all trust `release.yml`.

- [ ] **Step 4: Run the focused documentation and workflow tests**

```bash
uv run --frozen pytest tests/scripts/test_release_workflows.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the publisher documentation**

```bash
git add docs/maintainers/releasing.md tests/scripts/test_release_workflows.py
git commit -m "docs: record ali publisher migration"
```

---

### Task 4: Run Complete Local Release Validation

**Files:**
- Verify only; modify implementation files solely to fix a demonstrated failure, repeating that failure's test first.

**Interfaces:**
- Consumes: the complete 21-package workspace and release workflows.
- Produces: validated wheels and source distributions suitable for TestPyPI rehearsal.

- [ ] **Step 1: Run formatting, lock, synchronization, and version checks**

```bash
uv lock --check
uv sync --frozen --all-groups --all-packages
uv run --frozen python -m black --check packages tests scripts
uv run --frozen python -m isort --check-only packages tests scripts
uv run --frozen python scripts/sync_version.py --check
```

Expected: all commands succeed.

- [ ] **Step 2: Run all non-conformance tests**

```bash
uv run --frozen python -m pytest -m 'not conformance'
```

Expected: all selected tests pass.

- [ ] **Step 3: Run replay-mode conformance tests**

```bash
uv run --frozen python -m pytest -m conformance --record-mode=none
```

Expected: all supported replay tests pass; credential-dependent cases may be skipped as already defined by the suite.

- [ ] **Step 4: Build and validate the exact artifact set**

```bash
build_dir=$(mktemp -d)
uv build --all-packages --no-sources --out-dir "$build_dir"
test "$(find "$build_dir" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')" = 21
test "$(find "$build_dir" -maxdepth 1 -name '*.tar.gz' | wc -l | tr -d ' ')" = 21
uv run --frozen python -m twine check "$build_dir"/*
uv run --frozen python -m scripts.validate_release_artifacts "$build_dir"
uv run --frozen python -m scripts.smoke_test_release_artifacts "$build_dir"
```

Expected: exactly 21 wheels and 21 source distributions pass metadata, license, dependency, and isolated-install validation.

- [ ] **Step 5: Run final repository invariants**

```bash
uv run --frozen pytest tests/scripts -v
retired=$(printf '%s%s' ali baba)
test -z "$(git ls-files | rg -i "$retired")"
test -z "$(git grep -Il -i "$retired")"
git diff --check
git status --short
```

Expected: tests pass, both searches are empty, `git diff --check` succeeds, and the worktree has no uncommitted changes.

- [ ] **Step 6: Push the validated commits to `main`**

```bash
git push origin main
```

Expected: push succeeds and GitHub starts main CI plus Release Please synchronization.

- [ ] **Step 7: Verify remote CI without merging the release PR**

```bash
gh run list --repo salesforce/multicloud-py --branch main --limit 5
gh pr checks 10 --repo salesforce/multicloud-py
```

Expected: main CI and PR #10 CI pass. If bot-authored PR CI is `action_required`, approve only that CI run; do not merge the PR.

---

### Task 5: User Handoff — Bootstrap the Three New TestPyPI Projects

**Files:**
- External configuration only; no repository changes expected.

**Ownership:** User-operated. The agent provides the exact fields and validation guidance but performs no registration.

**Interfaces:**
- Consumes: `.github/workflows/test-pypi.yml` batches 3, 6, and 7.
- Produces: three active TestPyPI projects with trusted publishers and installable artifacts.

- [ ] **Step 1: Create or protect the three GitHub TestPyPI environments**

Create:

```text
testpypi-multicloud-py-blob-ali
testpypi-multicloud-py-pubsub-ali
testpypi-multicloud-py-sts-ali
```

Apply the same protection and reviewer settings as the existing TestPyPI environments.

- [ ] **Step 2: Register the three pending TestPyPI publishers**

Use owner `salesforce`, repository `multicloud-py`, workflow `test-pypi.yml`, and the exact environment corresponding to each project:

```text
multicloud-py-blob-ali       -> testpypi-multicloud-py-blob-ali
multicloud-py-pubsub-ali     -> testpypi-multicloud-py-pubsub-ali
multicloud-py-sts-ali        -> testpypi-multicloud-py-sts-ali
```

- [ ] **Step 3: Run TestPyPI batches 3, 6, and 7 individually**

```bash
gh workflow run test-pypi.yml --repo salesforce/multicloud-py -f batch=3
gh workflow run test-pypi.yml --repo salesforce/multicloud-py -f batch=6
gh workflow run test-pypi.yml --repo salesforce/multicloud-py -f batch=7
```

Wait for each run before starting the next if an environment requires approval. Expected: existing immutable artifacts are skipped and each new project receives one wheel and one source distribution.

- [ ] **Step 4: Verify TestPyPI project metadata and isolated installation**

For each new project, verify the expected version is visible at `https://test.pypi.org/project/<project>/`, then install it in a fresh temporary virtual environment using TestPyPI plus production PyPI as the dependency fallback.

Expected: all three distributions install and expose their `ali` entry point.

---

### Task 6: User Handoff — Configure Production Publishers and Release

**Files:**
- External configuration and release operation only.

**Ownership:** User-operated. The agent stops after repository validation and does not create publishers or merge the production release without a new explicit request.

**Interfaces:**
- Consumes: three new PyPI project identities, 18 retained PyPI projects, `release.yml`, and PR #10.
- Produces: one GitHub release and 21 production PyPI projects at the release version.

- [ ] **Step 1: Create or protect the three production GitHub environments**

Create:

```text
pypi-multicloud-py-blob-ali
pypi-multicloud-py-pubsub-ali
pypi-multicloud-py-sts-ali
```

Apply the same required-reviewer and branch/tag restrictions as the existing production environments.

- [ ] **Step 2: Register the three pending production publishers**

Use owner `salesforce`, repository `multicloud-py`, workflow `release.yml`, and:

```text
multicloud-py-blob-ali       -> pypi-multicloud-py-blob-ali
multicloud-py-pubsub-ali     -> pypi-multicloud-py-pubsub-ali
multicloud-py-sts-ali        -> pypi-multicloud-py-sts-ali
```

- [ ] **Step 3: Audit the full 21-project production authorization set**

Verify:

```text
18 retained projects: workflow release.yml, environment pypi-<exact-project-name>
3 new projects: pending workflow release.yml, environment pypi-<exact-project-name>
3 legacy projects: retained under Salesforce ownership but absent from release.yml
```

Expected: every project in the current `release.yml` matrix has exactly matching authorization.

- [ ] **Step 4: Obtain explicit user confirmation before production publication**

Present the completed TestPyPI evidence, the 21-project authorization audit, and PR #10 checks. Do not merge based only on plan approval.

- [ ] **Step 5: Merge PR #10 after confirmation**

```bash
gh pr merge 10 --repo salesforce/multicloud-py --merge
```

Expected: Release Please creates the release tag and GitHub release, validates 42 artifacts, and starts the 21-project production publish matrix in the same `release.yml` run.

- [ ] **Step 6: Monitor the complete release workflow**

```bash
gh run list --repo salesforce/multicloud-py --workflow release.yml --limit 5
```

Open the run corresponding to the PR #10 merge and wait until validation and all 21 publish jobs succeed.

- [ ] **Step 7: Verify final release state**

Confirm:

```text
GitHub release tag and release page exist for the expected version.
All 21 current PyPI projects expose that exact version.
Each of the three new Ali projects has one wheel and one source distribution.
The three legacy projects remain owned and unchanged.
The repository and release artifacts contain no retired provider token.
```

If any publish job fails after some projects succeed, rerun only failed jobs; `skip-existing: true` safely ignores artifacts already uploaded.
