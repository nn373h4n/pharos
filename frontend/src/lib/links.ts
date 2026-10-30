// Links in the text of an article come from third parties (feeds, search results). The backend
// stores only http(s) addresses without credentials, and the browser checks again before it makes
// a link: a `javascript:` or `data:` address must never reach an `href`.

const MAX_LENGTH = 2048;
// White space of any kind, control characters, quotes, angle brackets, backslashes and lone
// surrogates: the same set the backend refuses (ingest/links.py), so a link that passes there
// passes here.
const FORBIDDEN = /[\s\u0000-\u0020\u007f-\u009f<>"\\\ud800-\udfff]/;

/** The address as an absolute http(s) URL that is safe to put in `href`, or null. */
export function safeHref(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_LENGTH) return null;
  if (FORBIDDEN.test(value)) return null;
  // The browser's URL parser reads "https:///x" as "https://x/"; the backend reads it as a link
  // without a host. A host must follow the two slashes directly.
  if (!/^https?:\/\/[^/?#]/i.test(value)) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (!url.hostname || url.username || url.password) return null;
  return url.href;
}

/**
 * The host of a link that passed `safeHref`, with its port, shown next to it so people see where
 * it goes: `example.org:22` is not the same place as `example.org`.
 */
export function hostOf(href: string): string {
  return new URL(href).host;
}
