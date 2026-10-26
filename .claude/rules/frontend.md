---
paths:
  - "frontend/**/*.{ts,tsx}"
---

# Frontend rules

- No hard-coded user-visible text: add keys to `messages/en.json` (source) and to the az, tr and ru files in the same change.
- Server components for read pages; call the API on the same origin (`/api`). No tokens or secrets in the browser.
- Use shadcn/ui (Radix) components; every interactive element works with the keyboard and has an accessible name.
- Design loading, empty, error, forbidden and budget-exhausted states.
- Hiding UI is never authorization; the backend decides.
- Test user flows with Playwright (with axe) and logic with vitest; avoid snapshot-only tests.
