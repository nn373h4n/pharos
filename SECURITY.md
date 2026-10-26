# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Include what you found, the affected version or commit, steps to reproduce, and the impact you expect. Do not include real secrets or personal data.

We aim to acknowledge a report within 5 working days and to share a plan or fix timeline within 15 working days. Pharos is maintained by volunteers, so these are goals, not guarantees. We credit reporters in the release notes unless they prefer to stay anonymous.

## Scope

In scope: the code in this repository, its Docker images and its default configuration. Examples: authentication or session flaws, tenant isolation (`org_id`, row-level security), SSRF through connectors or user-added sources, prompt injection that leads to unsafe output, XSS or CSRF, and leaked secrets.

Out of scope: vulnerabilities in third-party services or dependencies without a Pharos-specific impact (report those upstream), issues that need a compromised host or administrator account, and findings from automated scanners without a demonstrated impact.

## Supported versions

Pharos has no stable release yet. Fixes land on `main`. This section will list supported release lines once versions are published.

## Safe harbor

If you act in good faith, avoid privacy violations and service disruption, and give us reasonable time to respond before disclosure, we will not take or support legal action against you.

## How we work on security

The [threat model](docs/THREAT_MODEL.md) lists the assets, trust boundaries and controls. Every control is meant to have an automated test, and the repository is scanned for secrets (gitleaks), vulnerable dependencies and container issues in CI.
