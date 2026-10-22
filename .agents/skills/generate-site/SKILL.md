---
name: generate-site
description: Use when previewing, validating, publishing, or diagnosing the MultiCloud-Py documentation website and its branch-based GitHub Pages deployment.
---

# Build and publish the documentation website

Read `AGENTS.md`, `mkdocs.yml`, `.github/workflows/docs.yml`, and
`docs/maintainers/website.md`. Sources live on `main` in `docs/`. API pages
are rendered from source docstrings by mkdocstrings. GitHub Pages serves
static output committed to the `site` branch's `/docs` folder.

## Preview and validate

Use a current, clean checkout or isolated worktree; preserve existing work.
From the repository root:

```bash
uv run --frozen --group docs mkdocs build --strict
uv run --frozen --group docs mkdocs serve
```

Use the local URL printed by MkDocs. Fix errors in source rather than
suppressing strict warnings. Inspect rendered pages, changed links, and
Python imports. Do not commit generated output to `main`: its `docs/` tree
contains Markdown sources, and `site/` is ignored. Content changes use
`multi-cloudpy-docs-guides` and a source PR against `main` first.

## Prepare a publishing PR

The Documentation Actions workflow validates source builds but does not
publish. Merging a source PR into `main` alone does not update the website.

Follow the publishing commands in `docs/maintainers/website.md`:

1. Fetch the public repository's `main` and `site` branches. Resolve the
   exact merged source SHA and build it in an isolated source worktree.
2. Build with `--strict --site-dir` into a fresh temporary directory.
3. Create a unique publishing branch and worktree from the existing `site`
   branch. Replace only that isolated worktree's generated `/docs` tree
   with the fresh output, including `docs/.nojekyll`.
4. Review added, changed, and removed pages and check local links. Preserve
   files outside the generated tree. If the diff is empty, stop.
5. Commit with the source SHA, push the publishing branch to an authorized
   remote, and open a PR against `salesforce/multicloud-py:site`. Use a fork
   if upstream branch creation is unavailable; use `OWNER:BRANCH` as the PR
   head in that case.

Do not use `mkdocs gh-deploy`, orphan or force-replace an existing publishing
branch, reset someone's checkout, or overwrite `main/docs` with HTML.
Bootstrap a missing `site` branch only as an explicitly authorized initial
setup; do not silently change the destination to `gh-pages` or `main`.

## Verify publication

After the publishing PR merges, Pages builds `site:/docs`. Confirm with:

```bash
gh api repos/salesforce/multicloud-py/pages
gh api repos/salesforce/multicloud-py/pages/builds/latest
```

The configuration should have `build_type: legacy`, source branch `site`,
and path `/docs`. A GitHub-provided Pages build can appear in Actions even
though the publication source is a branch. Rerunning the Documentation
workflow only validates source; it does not publish the site.

For initial migration, first merge the change that removes Actions artifact
deployment and let older deployments finish. Once a validated `site:/docs`
snapshot exists, an authorized administrator selects Settings → Pages →
**Deploy from a branch**, **site**, **/docs**. Do not change repository settings
or merge PRs without authorization for those actions.

A Pages API 404 may indicate missing setup or insufficient access. Confirm
permissions and report the exact blocker rather than claiming deployment.
The canonical URL is `https://opensource.salesforce.com/multicloud-py/`;
leave its inherited organization domain alone and do not add a repository
`CNAME`. Verify the homepage and at least one guide after a successful build.

Report the source SHA, publishing PR, strict-build result, Pages build status,
and verified URL. Distinguish a local preview from a published website.
