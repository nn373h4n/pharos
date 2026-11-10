# Releasing Pharos

For the maintainer. A release is a tag `vX.Y.Z` on a commit of `main`; the release workflow
(`.github/workflows/release.yml`, see [DEPLOYMENT.md](../DEPLOYMENT.md#release-images-githubworkflowsreleaseyml))
builds, scans, publishes and signs the two images. The workflow never creates a tag and never
publishes without one. Nothing here may be done by an assistant without your explicit approval for
that release.

## Before the first tag

These are repository settings the workflow cannot enforce, and the first two matter for safety:

- A **tag ruleset** for `v*`: only you can create a tag, and nobody can update or delete one. Without
  it a deleted and recreated tag could point a release at another commit.
- **Actions pinned to a full commit SHA** turned on in the repository's Actions settings.
- After the first push, set both packages (`pharos-backend`, `pharos-web`) to **public** and link
  them to this repository; then check that `scripts/verify-release.sh` works without logging in.
- Look at the readiness list of the milestone you are releasing (`TASKS.md`, `PROGRESS.md`) and write
  what is not ready into the release notes. Tasks that are still open are never described as done.

## Steps

1. **Prepare the release.** In one pull request: raise the version in `backend/pyproject.toml` and
   `frontend/package.json` to `X.Y.Z` (and run `uv lock` so `uv.lock` agrees), and cut
   `CHANGELOG.md`: rename `Unreleased` to `[X.Y.Z] - <date>`, add a new empty `Unreleased` above it,
   and add the upgrade notes (what an operator must do, or "none"). Merge it. The tag check refuses
   a tag whose version differs from the code's.
2. **Tag the merge commit** on `main`: `git tag vX.Y.Z <commit>`, then push the tag. The `Release`
   workflow starts: build, scan, SBOM, publish, sign. If a step fails, fix the cause and use
   "Re-run failed jobs"; if `publish` already succeeded, never delete the tag or the images (a
   release is never overwritten; a full re-run stops at that check).
3. **Verify** from a machine that is not the runner:
   `scripts/verify-release.sh <owner>/<repo> vX.Y.Z <commit-sha>`. It prints the digest of each
   image. If it fails, the release is not announced.
4. **Publish the release notes** (a GitHub Release on the tag) from the template below, with the two
   digests and the commit from step 3.

## Release notes template

````markdown
## Pharos vX.Y.Z

Release commit: `<commit-sha>`

### Images

- `ghcr.io/<owner>/pharos-backend@sha256:<digest>`
- `ghcr.io/<owner>/pharos-web@sha256:<digest>`

### Verify before you run

```bash
git clone https://github.com/<owner>/<repo> && cd pharos && git checkout vX.Y.Z
scripts/verify-release.sh <owner>/<repo> vX.Y.Z <commit-sha> \
  --backend ghcr.io/<owner>/pharos-backend@sha256:<digest> \
  --web ghcr.io/<owner>/pharos-web@sha256:<digest>
```

The two flags make the script compare the references you are about to run with the release's digests.
Run the images by digest, as in "Installing a published release" in `docs/DEPLOYMENT.md`.
A passing check means both images were signed, and given an SBOM attestation, by this repository's
release workflow for this tag and commit. It does not say the commit is on `main`, that the SBOM is
complete, or that the images have no vulnerability.

### What is in this release

(From `CHANGELOG.md`.)

### Not ready in this release

(Open tasks and gates that matter to an operator, stated plainly.)

### Upgrading

(From the changelog's upgrade notes. For a first release: "none, this is the first release".)

### Rolling back

Set `PHAROS_BACKEND_IMAGE` and `PHAROS_WEB_IMAGE` to the previous release's digests and run
`docker compose ... up -d` again. If the newer version changed the database schema, restore the
backup taken before the upgrade (`docs/DEPLOYMENT.md`, "Backup and restore").
````

## What a release does not do

It does not install anything anywhere, create a tag, or change repository settings. The pilot's
own configuration (single sign-on, keys, budget, sources) stays in the deployment's `.env` and the
admin panel, never in a release.
