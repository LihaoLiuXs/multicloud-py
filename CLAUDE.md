# Repository guidance

MultiCloud-Py is a Python 3.10+ SDK with a uv workspace. The root
`pyproject.toml` is tooling configuration, not a published distribution.
Source lives in `packages/<service>/<distribution>/multicloud/`. Distribution
names use `multicloud-py-`; imports use the `multicloud.*` namespace.

## Architecture and API

- Preserve the Client → Driver → Provider architecture. The public client
  forwards to a driver whose public methods validate and normalize arguments,
  then call provider `do_*` hooks.
- Keep client/common packages independent of provider packages and native cloud
  SDKs. Providers must not depend on or reference sibling providers in code or
  comments. Keep client/driver documentation and tests cloud-agnostic.
- The public client owns exception translation: catch provider failures there
  and raise the result of `translate_error(exc)` with exception chaining.
  Provider hooks let native exceptions propagate unless a catch is needed for
  cleanup, operation control flow, or deferred iteration. Keep shared mapping
  functions limited to classification, with request-aware behavior at the
  operation boundary. Preserve already translated exceptions.
- Use direct operation arguments, keyword-only optional parameters, typed
  results, and existing dataclass conventions. Do not introduce request wrappers
  or expose native clients through the public API.
- Provider discovery uses installed distribution entry points and service
  registries. Check each service's implementation rather than adding eager
  imports to implicit namespace packages.
- Use the provider spelling `ali` in identifiers and `Ali` in prose. The
  repository-wide naming test also scans documentation and skills.
- Ali distributions are currently placeholders. Do not claim provider support
  or fabricate recordings for an implementation that is not present.
- Import clients from their actual exports, for example
  `from multicloud.blob.blob_client import BucketClient`.

## Development and validation

Use uv and the committed lockfile. CI configuration in `.github/workflows/`
is authoritative for required checks; the Makefile provides additional local
targets. Use GitHub PRs against `salesforce/multicloud-py:main`, with conventional
commit titles. This repository does not require an internal work-item prefix.

```bash
uv sync --frozen --all-groups --all-packages
uv run --frozen python -m black --check packages tests scripts
uv run --frozen python -m isort --check-only packages tests scripts
uv run --frozen python scripts/sync_version.py --check
uv run --frozen python -m pytest -m 'not conformance'
uv run --frozen python -m pytest -m conformance --record-mode=none
uv run --frozen --group docs mkdocs build --strict
```

Run checks appropriate to the change. Unit tests mock SDK collaborators, not
the provider under test. Assert observable behavior and specific errors.
Conformance suites define shared behavior once and run concrete provider
classes in replay mode. Add happy and failure paths for new operations,
verify round trips, use unique resource names, and clean up resources.

Recording with `--record-mode=rewrite` and the E2E harness use live cloud
resources. Establish the intended test account, resources, and credentials
before running them. Keep credentials in the provider's credential chain;
never print or commit them. Inspect recorded diffs for secrets. Do not invent
fixtures or edit cassettes to hide an implementation failure.

## Repository skills

The canonical skills live in `.agents/skills/`. Claude Code discovers the
same files through the `.claude/skills` compatibility symlink. Edit only the
canonical tree; do not create separate tool-specific skill copies.

| Skill | Use |
| --- | --- |
| `multicloudpy-feature-dev` | Cross-provider operations, API extensions, and conformance tests |
| `multi-cloudpy-docs-guides` | Usage guides and API-reference source updates |
| `generate-site` | Preview, validate, and prepare a publishing PR to the `site` branch |
| `onboard-e2e-example` | Add a service lifecycle to the live E2E harness |

Use the feature-development skill before implementing a new SDK operation.
Read only skills relevant to the task. Codex explicit invocation uses
`$skill-name`; Claude Code uses `/skill-name`.

## Website

Markdown sources live on `main` in `docs/`, configured by `mkdocs.yml`. The
Documentation workflow validates source builds. Pages serves generated HTML
from the `site` branch's `/docs` folder; publish through a separate PR against
`site` after source changes merge. Generated output must not replace Markdown
on `main`. The local `site/` build directory stays ignored. The canonical URL is
`https://opensource.salesforce.com/multicloud-py/`. See
`docs/maintainers/website.md` for setup and maintenance.
