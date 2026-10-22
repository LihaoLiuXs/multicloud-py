---
name: multi-cloudpy-docs-guides
description: Use when documenting implemented MultiCloud-Py features, updating service guides after a PR, or adding a MkDocs API-reference page.
---

# Update documentation sources

Read `AGENTS.md`. Documentation sources live in `docs/` on `main`, with
navigation and API-source search paths in `mkdocs.yml`. Read the actual client,
driver, tests, and examples for the feature before describing its behavior.
For a supplied PR, inspect its diff and confirm whether it has merged.

## Choose the source

- Usage guides: `docs/guides/` (blob, STS, and Pub/Sub guides currently exist).
- API-reference directives: `docs/api/`; API details come from source docstrings.
- Architecture and errors: `docs/design/`.
- Service overview and first example: `docs/index.md` and `docs/getting-started.md`.
- Planned work: `docs/roadmap.md`; distinguish planned and implemented support.

Verify files before editing them; add an absent service guide when requested
and wire it into `mkdocs.yml` and the relevant index. Group changes by service
when documenting several features.

## Write accurate guides

Use plain Markdown and MkDocs Material admonitions. Navigation belongs in
`mkdocs.yml`, not Jekyll frontmatter. A new service guide should explain its
purpose, actual provider support, installation, a quick start, common operations,
errors, and configuration. Mark Ali placeholders as planned.

Use published `multicloud-py-*` distribution names and actual import paths.
For example:

```python
from multicloud.blob.blob_client import BucketClient

with BucketClient(provider="aws", bucket="my-bucket", region="us-west-2") as client:
    client.upload(key="example.txt", payload=b"example")
```

Use direct parameters with keyword-only optionals, rather than request
wrappers. Verify signatures and imports against this checkout. Use context
managers where supported. Explain prerequisites and cleanup for examples
that create resources; never include real credentials or account identifiers.

When adding a whole service reference, create `docs/api/SERVICE.md` using
the existing mkdocstrings directive pattern, register its package path in
`plugins.mkdocstrings.handlers.python.paths`, add navigation, and update
`docs/api/index.md`. Individual method changes normally need docstrings,
not hand-written copies of generated API details.

## Validate and submit

```bash
uv run --frozen --group docs mkdocs build --strict
uv run --frozen python -m pytest tests/scripts/test_ali_naming.py
git diff --check
```

Preview changed pages with `uv run --frozen --group docs mkdocs serve`.
Open a PR against `salesforce/multicloud-py:main`, using a conventional
`docs:` title and describing what is documented and how it was validated.
Do not require internal work items. Generated `site/` stays out of Git;
the Documentation workflow validates source PRs. After the source PR merges
into `main`, it automatically commits the validated build to `site:/docs` and
requests a Pages deployment. No separate publishing PR is needed. Use
`generate-site` to diagnose a failed publication or prepare a manual fallback.
