# 0019. Model text is validated plain text; the Content-Security-Policy is strict

- Status: Proposed
- Date: 2026-11-01

## Context

The Home screen (M4-08) shows text a model wrote from a stranger's article: a summary, steps, a
prompt to copy and a note. ADR-0009 (still `Proposed`) planned to sanitize such text on the server
with `nh3`, to keep the safe part of any markup, and to add a strict Content-Security-Policy (F-03)
alongside.

M4-04 and M4-06 changed the situation. The product asks the model for plain text; every field is
checked on the way in and again on the way out (`pharos.core.plaintext`, `pharos.insights`): a link,
a bare domain, markup, an invisible, format or direction character, a request for a secret or for
personal data are refused, and the answer is dropped, not repaired. Nothing in the product renders
markdown or HTML from a model.

## Options considered

1. Follow ADR-0009: store sanitized HTML or markdown from the model. — Needs a sanitizer in the
   path of every displayed text, keeps markup alive, and a bug in the sanitizer becomes a stored
   cross-site-scripting hole. It also adds a dependency (`nh3`) for a feature the product does not
   have.
2. Validated plain text only, shown as text. — Smaller surface: there is no markup to clean, React
   escapes what it shows, and a text that does not pass is not shown at all.
3. Validated plain text, plus a strict Content-Security-Policy as the second wall. — As option 2, and
   if some other defect ever lets a script reach the page, the browser still refuses to run it.

## Decision

Option 3.

- **Model text** (summaries, categories, steps, prompts, notes) is plain text that passed the
  checks of the backend. The front end renders it only as React text nodes or as the value of a
  read-only text field, never with `dangerouslySetInnerHTML`, never as a link. Tests fail if a model
  text becomes an element.
- **Refuse, do not repair.** This supersedes the sanitization part of ADR-0009 for the product's own
  model output. `nh3` is needed only if a later feature chooses to render markup from a model; that
  feature would then need its own decision.
- **The policy** (`frontend/src/lib/csp.ts`, set by `src/proxy.ts` on every page): scripts only with
  a nonce made for the request and what those scripts load (`'strict-dynamic'`); no `unsafe-inline`
  and no `unsafe-eval` for scripts in production; workers only from the site, no frames; styles may be inline (React writes `style`
  attributes); `default-src`, images, fonts and `connect-src` are the site itself (images also
  `data:`); no plugins; `base-uri` and `form-action` the site itself; the page cannot be framed
  (`frame-ancestors 'none'`); insecure requests are upgraded when the visitor came over https. A path
  with a dot in it is not a page of the app: the proxy answers it itself with a plain-text 404 under
  the policy (Next's prebuilt 404 page could not carry the nonce). The development server
  is looser, as `next dev` needs.
- Pages are rendered per request, since a nonce must be made per request.

## Consequences

- A Content-Security-Policy is now on every page; tests load pages in a real browser and fail on any
  violation, and check that every script carries the nonce of the response.
- `style-src 'unsafe-inline'` remains. It cannot run code, but an injected style could change how
  the page looks (a phishing overlay). Removing it means replacing inline `style` attributes with
  classes, and is a later cleanup.
- HSTS is still not set; that belongs to the TLS front (Caddy) and the deployment guide.
- No static pages: a little more work per request, which is small for this application.
- If a deployment puts another proxy in front that adds its own policy, the two are combined by the
  browser (the stricter wins); the guide says to leave the policy to the web application.

## References

ADR-0009 (the sanitization plan this narrows), ADR-0015 (class A and what may be shown),
`docs/THREAT_MODEL.md`, F-03; `frontend/src/lib/csp.ts`, `frontend/src/proxy.ts`,
`frontend/e2e/csp.spec.ts`, `backend/src/pharos/insights/validator.py`.
