# Maintaining the website

The documentation website uses MkDocs Material. Markdown sources live in
`docs/`, navigation and rendering settings in `mkdocs.yml`, and locked tooling
in the `docs` dependency group in `pyproject.toml` and `uv.lock`.

## Preview and validate

From the repository root:

```bash
uv run --frozen --group docs mkdocs serve
uv run --frozen --group docs mkdocs build --strict
```

The preview is available at the local address printed by MkDocs. The build
writes static files to `site/`; keep that generated directory out of Git.
API reference pages are generated from source docstrings with mkdocstrings.
Internal planning documents under `docs/superpowers/` are excluded.

## Publication

The Documentation workflow builds pull requests and commits to `main` in strict
mode. After a successful build on `main`, it uploads `site/` and deploys the
artifact to GitHub Pages. Pull requests never deploy. A manual workflow run
on `main` can republish the website.

In repository **Settings → Pages**, select **GitHub Actions** as the build and
deployment source. The deployment uses the `github-pages` environment and
GitHub's built-in token; no separate deployment secret is required.

The canonical URL is <https://opensource.salesforce.com/multicloud-py/>.
The Salesforce organization Pages domain is inherited by project sites, as
with MultiCloudJ. Do not set a repository custom domain or add a `CNAME` for
`opensource.salesforce.com`; the shared domain belongs to the organization site.
Confirm the URL returned by Pages when enabling publication.
