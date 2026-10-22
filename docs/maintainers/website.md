# Maintaining the website

The documentation website uses MkDocs Material. Markdown sources live on
`main` in `docs/`, navigation and rendering settings in `mkdocs.yml`, and
locked tooling in the `docs` dependency group in `pyproject.toml` and `uv.lock`.

GitHub Pages serves generated HTML from the **`site` branch's `/docs` folder**,
matching the publishing-source layout used by MultiCloudJ. The Documentation
Actions workflow validates source builds; it does not deploy the website.

## Preview and validate

From the repository root:

```bash
uv run --frozen --group docs mkdocs serve
uv run --frozen --group docs mkdocs build --strict
```

Use the preview URL printed by MkDocs. The default build writes to the ignored
`site/` directory. API reference pages are generated from source docstrings
with mkdocstrings; planning documents under `docs/superpowers/` are excluded.
Do not commit generated output to `main` or replace its Markdown `docs/` tree.

## Publish a new version

First merge documentation or SDK changes into `main`. Build from that merged
commit in a clean checkout or isolated worktree; preserve existing local work.
Use a fresh temporary build directory and a separate publishing worktree.

The following commands assume `origin` is the public repository and an existing
`site` branch has already been initialized. Run them in a dedicated shell and
keep that shell open through review and submission so the variables remain
available. Fail-fast mode prevents a failed build from reaching publication.

```bash
set -e
git fetch origin main site
SOURCE_SHA=$(git rev-parse origin/main)
BUILD_DIR=$(mktemp -d)
SOURCE_DIR=$(mktemp -d)/source
PUBLISH_DIR=$(mktemp -d)/publish
PUBLISH_BRANCH="codex/publish-site-$(git rev-parse --short origin/main)-$(date +%Y%m%d%H%M%S)"

git worktree add --detach "$SOURCE_DIR" "$SOURCE_SHA"
(cd "$SOURCE_DIR" && uv run --frozen --group docs mkdocs build --strict --site-dir "$BUILD_DIR")

git worktree add -b "$PUBLISH_BRANCH" "$PUBLISH_DIR" origin/site
# This isolated worktree contains generated website files only.
git -C "$PUBLISH_DIR" rm -r --ignore-unmatch docs
mkdir -p "$PUBLISH_DIR/docs"
cp -R "$BUILD_DIR/." "$PUBLISH_DIR/docs/"
touch "$PUBLISH_DIR/docs/.nojekyll"
git -C "$PUBLISH_DIR" add docs
git -C "$PUBLISH_DIR" diff --cached --stat
```

Review the complete generated change, verify the homepage and at least one
API page, and confirm that obsolete generated pages are removed. If the staged
diff is empty, no publish is needed. Otherwise:

```bash
git -C "$PUBLISH_DIR" commit -m "docs: publish site from $SOURCE_SHA"
git -C "$PUBLISH_DIR" push origin "$PUBLISH_BRANCH"
gh pr create --repo salesforce/multicloud-py --base site --head "$PUBLISH_BRANCH" \
  --title "docs: publish website" \
  --body "Publishes the strict MkDocs build from main commit $SOURCE_SHA."
```

If you cannot create branches upstream, push to your fork remote instead of
`origin` and use `--head "OWNER:$PUBLISH_BRANCH"` when opening the PR, replacing
`OWNER` with the fork owner's GitHub login.

After the publishing PR merges into `site`, GitHub Pages deploys its `/docs`
folder. `.nojekyll` makes Pages serve the generated files without a Jekyll
transformation. No force-push or `mkdocs gh-deploy` is needed. Use normal Git
worktree cleanup after retaining any needed review artifacts.

## One-time switch from Actions deployment

1. Merge the change that removes artifact deployment from the Documentation
   workflow. Wait for any older in-flight Actions deployments to finish.
2. Initialize `site` with the validated static build in `/docs`, including
   `/docs/.nojekyll`, if that branch does not yet exist.
3. In repository **Settings → Pages**, choose **Deploy from a branch**,
   select **site**, select **/docs**, and save.
4. Check the Pages build and the public homepage and guides. Enable
   **Enforce HTTPS** when available.

The canonical URL remains <https://opensource.salesforce.com/multicloud-py/>.
The organization domain is inherited; leave the repository custom-domain field
empty and do not add a `CNAME` for `opensource.salesforce.com`.

Verify the configuration and deployment with:

```bash
gh api repos/salesforce/multicloud-py/pages
gh api repos/salesforce/multicloud-py/pages/builds/latest
```

The configuration should report `build_type: legacy` and source branch `site`,
path `/docs`. GitHub may display its own Pages build as an Actions run even
with this branch-based setting; it is separate from our validation workflow.
