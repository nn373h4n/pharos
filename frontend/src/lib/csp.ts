// The Content-Security-Policy of the web app (F-03, ADR-0019). One function builds it, so a test
// can pin every directive. Scripts are allowed only with the nonce made for the request (and what
// those scripts load, 'strict-dynamic'); styles may be inline because React writes `style`
// attributes (the budget bar of the model page); nothing else may be inline.

export type CspOptions = {
  nonce: string;
  /** `next dev` evaluates code and talks to its own hot-reload socket. */
  development?: boolean;
  /**
   * The request came over https. Only then are insecure requests upgraded: on plain http the
   * upgrade would break a page served that way (a direct visit to the web container on a local
   * network, for example).
   */
  secure?: boolean;
};

export function buildCsp({ nonce, development = false, secure = true }: CspOptions): string {
  const script = ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"];
  if (development) script.push("'unsafe-eval'");
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": script,
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:"],
    "font-src": ["'self'"],
    "connect-src": development ? ["'self'", "ws:", "wss:"] : ["'self'"],
    "object-src": ["'none'"],
    "worker-src": ["'self'"],
    "frame-src": ["'none'"],
    "manifest-src": ["'self'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
  };
  const parts = Object.entries(directives).map(([name, values]) => `${name} ${values.join(" ")}`);
  if (!development && secure) parts.push("upgrade-insecure-requests");
  return parts.join("; ");
}

/** A fresh, unguessable value for every request (128 bits, base64). */
export function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}
