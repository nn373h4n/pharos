## Summary

What changes and why. Link the issue or task (for example `M1-05`).

## Checks

Mark each as PASS, FAIL or NOT RUN, with a reason for anything not run.

- [ ] `make lint`
- [ ] `make test`
- [ ] New or changed behavior has tests
- [ ] User-visible strings added to all four locales (`en`, `az`, `tr`, `ru`)
- [ ] Docs updated (`docs/`, `.env.example`, `CHANGELOG.md` where relevant)
- [ ] ADR added or updated if this changes architecture, data model, a security boundary or deployment

## Security

- [ ] No secrets, real company data or personal data
- [ ] Tenant scoping respected (`org_id` from the session only)
- [ ] External content and LLM output are treated as untrusted
- [ ] No new dependency, or its reason is given above

## Rollback

How to undo this change.

---

Commits are signed off (`git commit -s`).
