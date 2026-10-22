# Maintaining the website

The documentation website uses MkDocs Material. Markdown sources live on
`main` in `docs/`, navigation and rendering settings in `mkdocs.yml`, and
locked tooling in the `docs` dependency group in `pyproject.toml` and `uv.lock`.

GitHub Pages serves generated HTML from the **`site` branch's `/docs` folder**,
matching the publishing-source layout used by MultiCloudJ. The Documentation
Actions workflow validates pull requests and automatically publishes successful
builds after changes merge into `main`. The Pages source setting is permanent;
it does not need to be selected again for each update.

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

## Automatic publishing

Merge a documentation or SDK source PR into `main`. The Documentation workflow:

1. Builds the exact merged commit with the locked documentation dependencies
   and `mkdocs build --strict`.
2. Transfers the validated output to a separate publishing job, which replaces
   only the `site` branch's generated `/docs` tree and updates `SOURCE_COMMIT`.
3. Pushes a normal commit to `site`, preserving its history and `/docs/.nojekyll`.
4. Explicitly requests a GitHub Pages build, which deploys `site:/docs`.

No separate generated-site PR is needed. Pull requests only validate the build;
they do not receive publishing permissions. Publishing jobs run one at a time
and skip an older source commit when `main` has advanced. The publishing job
uses the built-in `GITHUB_TOKEN` with `contents: write` and `pages: write`;
no personal access token is needed. GitHub does not automatically start a
Pages build for commits pushed with that token, so the explicit build request
is required. See [GitHub's publishing-source documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site#troubleshooting-publishing-from-a-branch)
and [Pages build API](https://docs.github.com/en/rest/pages/pages#request-a-github-pages-build).

Check the **Documentation** run first, then the **pages build and deployment**
run. A successful source build alone does not confirm that Pages has deployed.
If publishing fails, fix the reported configuration or permission error and
rerun the workflow on current `main` using **Actions → Documentation → Run
workflow**. A manual run on another branch validates without publishing. A
retry requests deployment even when the generated files are already current.

The repository must allow the publishing job to write to `site`. If the
`github-pages` environment restricts deployment branches, it must allow `main`,
where the publishing job runs, and `site`, where GitHub's Pages build runs.
Keep Pages configured to **Deploy from a branch → site → /docs**. The workflow
checks that setting and reports an error if it differs.

## Manual publishing fallback

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
printf '%s\n' "$SOURCE_SHA" > "$PUBLISH_DIR/SOURCE_COMMIT"
git -C "$PUBLISH_DIR" add docs SOURCE_COMMIT
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

1. Wait for any older in-flight Actions artifact deployments to finish.
2. Initialize `site` with the validated static build in `/docs`, including
   `/docs/.nojekyll`, if that branch does not yet exist.
3. In repository **Settings → Pages**, choose **Deploy from a branch**,
   select **site**, select **/docs**, and save.
4. Allow `main` and `site` in the `github-pages` environment's branch rules
   if that environment restricts deployment branches.
5. Check the Pages build and the public homepage and guides. Enable
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
with this branch-based setting; it follows our Documentation publishing job.
The source selection remains saved for all future deployments.
