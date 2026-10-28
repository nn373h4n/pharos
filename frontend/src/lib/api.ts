// The browser side of the backend API (docs/API.md). Every call is same-origin: Caddy sends
// /api/* to the backend, so the session cookie travels without any cross-site setup.

export type Role = "admin" | "editor" | "member";

export type User = {
  id: string;
  display_name: string;
  email: string | null;
  role: Role;
  locale: string | null;
  department: string | null;
  last_login_at: string | null;
  onboarding_completed_at: string | null;
};

export type Session = { user: User; csrf_token: string };

export type AuthConfig = {
  local_login: "off" | "break-glass" | "on";
  oidc: { provider_name: string; login_url: string } | null;
};

/** An answer that was not a success. `code` is the stable code of docs/API.md, never shown as is. */
export class ApiProblem extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfter: number | null = null,
  ) {
    super(code);
    this.name = "ApiProblem";
  }
}

const MAX_NEXT = 512;

/**
 * Where to go after signing in: a path on this site and nothing else. A scheme, `//host`, a
 * backslash (browsers read it as a slash), control characters and API paths all become "/".
 * The backend applies the same rule to `next`; this keeps the browser from even trying.
 */
export function safeNext(value: string | null | undefined): string {
  if (!value || value.length > MAX_NEXT || !value.startsWith("/") || value.startsWith("//")) {
    return "/";
  }
  if (value.includes("\\") || /[\u0000-\u001f\u007f]/.test(value)) return "/";
  // Dot segments ("/en/../api/me", "/%2e%2e/api/me") would be resolved by the browser to a
  // different path than the one checked here, so they are refused outright.
  let path: string;
  try {
    path = decodeURIComponent(value.split(/[?#]/, 1)[0]!);
  } catch {
    return "/";
  }
  if (path.split("/").some((segment) => segment === "." || segment === "..")) return "/";
  if (path === "/api" || path.startsWith("/api/")) return "/"; // JSON, not pages
  return value;
}

type UnauthorizedListener = () => void;
const listeners = new Set<UnauthorizedListener>();

/** Called when any call is answered with 401 (except the sign-in itself): the session is gone. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

type Options = { method?: string; body?: unknown; csrf?: string; ignore401?: boolean };

async function request(path: string, options: Options = {}): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = options.csrf;
  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw new ApiProblem(0, "network_error");
  }
  if (response.ok) return response;
  let code = "http_error";
  try {
    const problem: unknown = await response.json();
    if (problem && typeof problem === "object" && "code" in problem) {
      const found = (problem as { code: unknown }).code;
      if (typeof found === "string") code = found;
    }
  } catch {
    // A proxy error page or an empty body: the status alone tells what happened.
  }
  const retry = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  if (response.status === 401 && !options.ignore401) listeners.forEach((listener) => listener());
  throw new ApiProblem(response.status, code, Number.isFinite(retry) ? retry : null);
}

function isSession(value: unknown): value is Session {
  if (!value || typeof value !== "object") return false;
  const { user, csrf_token: token } = value as { user?: unknown; csrf_token?: unknown };
  if (typeof token !== "string" || !user || typeof user !== "object") return false;
  const { display_name: name, role } = user as { display_name?: unknown; role?: unknown };
  return typeof name === "string" && (role === "admin" || role === "editor" || role === "member");
}

export async function getAuthConfig(): Promise<AuthConfig> {
  return (await request("/api/auth/config")).json();
}

/** The signed-in user, or null when there is no session. Any other failure is thrown. */
export async function getSession(): Promise<Session | null> {
  try {
    const body: unknown = await (await request("/api/me", { ignore401: true })).json();
    if (!isSession(body)) throw new ApiProblem(200, "malformed_response");
    return body;
  } catch (error) {
    if (error instanceof ApiProblem && error.status === 401) return null;
    throw error;
  }
}

export async function localLogin(username: string, password: string): Promise<Session> {
  const response = await request("/api/auth/local/login", {
    method: "POST",
    body: { username, password },
    ignore401: true, // here 401 means "wrong credentials", not "your session ended"
  });
  return response.json();
}

/** Ends the session. Answers where to go next to end the identity provider's session too. */
export async function logout(csrf: string): Promise<string | null> {
  const response = await request("/api/auth/logout", { method: "POST", csrf, ignore401: true });
  if (response.status === 204) return null;
  // The session is already gone on the server; an answer we cannot read only means there is no
  // provider page to visit.
  const body: unknown = await response.json().catch(() => null);
  const url =
    body && typeof body === "object" && "end_session_url" in body
      ? (body as { end_session_url: unknown }).end_session_url
      : null;
  return typeof url === "string" && /^https?:\/\//i.test(url) ? url : null;
}
