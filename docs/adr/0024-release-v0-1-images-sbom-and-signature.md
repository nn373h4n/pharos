# 0024. Release v0.1: published images, an SBOM and a signature

- Status: Proposed
- Date: 2026-11-04

## Context

There is no release, no tag and no published image. Every installation builds its own images from
source, so image signing protects nobody today (F-05, F-07). The pilot needs something an operator
can install by a runbook and verify, and a pilot upgrade needs a previous version to return to.
Supply-chain checks already run in CI (lockfiles installed with `--frozen`, CodeQL, gitleaks, Trivy,
actions pinned by commit); the missing parts are pinned base images, an SBOM per image and a
signature.

## Options considered

1. **Images on GHCR, an SBOM per image, keyless signing** — pros: the operator pulls by digest and
   can verify the signature; the SBOM comes from Trivy, which CI already runs. Cons: the signing
   tool is a new tool in CI.
2. **Images on GHCR and an SBOM, no signature yet** — pros: no new tool. Cons: nothing proves the
   image came from this repository's workflow.
3. **A source tag only** — pros: simplest. Cons: every operator builds their own image; nothing is
   verifiable.

## Decision

Option 1.

- Base images and the `uv` and `pnpm` tools are pinned by digest or hash, kept fresh by Dependabot.
- A tag workflow builds the two images, scans them, attaches an SBOM to each, signs them keylessly,
  and publishes them to GHCR. The release notes carry the verify command, and the pilot pulls by
  digest.
- The CHANGELOG is cut to 0.1.0 with upgrade notes. The rollback is the previous tag plus a
  restore of the backup (ADR-0022).
- The signing tool is **new in CI and is asked for again at M5-10** before it is added. If it is
  not approved, the release ships Option 2 and says so in the notes.

## Consequences

- Positive: a verifiable artifact for the pilot; pinned bases; an SBOM that answers "what is in
  this image".
- Negative: publishing means an image registry and a workflow with permission to write packages
  and to sign, which is a new attack surface that the security review of M5 must cover.
- Follow-up: M5-10 builds the workflow and the verify step; a mutation check proves that an
  unsigned or tampered image does not pass verification (if signing is approved).

## References

- F-05, F-07 in `TASKS.md`; `docs/plans/m5-pilot-readiness.md` (M5-10, D4);
  `docs/ARCHITECTURE_PROPOSAL.md` section 7 (supply chain).
