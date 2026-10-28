import type { BrowserContext, Page } from "@playwright/test";

// The end-to-end tests run the production build of the web app alone, without the backend, so
// the API is answered here. Every answer has the shape documented in docs/API.md.

export type MockOptions = {
  /** Starts with a session (the cookie of a signed-in person). */
  signedIn?: boolean;
  localLogin?: "off" | "break-glass" | "on";
  /** Name of the single sign-on provider; null means none is configured. */
  provider?: string | null;
  /** What the password sign-in answers: success, wrong credentials or too many attempts. */
  loginResult?: "ok" | "wrong" | "limited";
  role?: "admin" | "editor" | "member";
  /** Sign-out of a single sign-on session answers with the provider's sign-out page. */
  providerLogoutUrl?: string;
};

export const CSRF_TOKEN = "csrf-e2e";
export const USER_NAME = "Test User";

export type MockState = {
  signedIn: boolean;
  /** Every call the page made, as "METHOD /path". */
  calls: string[];
  /** The X-CSRF-Token header of the last sign-out call. */
  logoutCsrf: string | null;
};

export async function mockApi(
  target: Page | BrowserContext,
  options: MockOptions = {},
): Promise<MockState> {
  const state: MockState = { signedIn: options.signedIn ?? false, calls: [], logoutCsrf: null };
  const user = {
    id: "00000000-0000-4000-8000-000000000001",
    display_name: USER_NAME,
    email: null,
    role: options.role ?? "member",
    locale: null,
    department: null,
    last_login_at: null,
    onboarding_completed_at: null,
  };
  const problem = (status: number, code: string, headers: Record<string, string> = {}) => ({
    status,
    headers: { ...headers, "Content-Type": "application/problem+json" },
    body: JSON.stringify({ code, status }),
  });

  await target.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    state.calls.push(`${request.method()} ${path}`);

    if (path === "/api/auth/config") {
      const provider = options.provider === undefined ? "Contoso" : options.provider;
      return route.fulfill({
        json: {
          local_login: options.localLogin ?? "on",
          oidc: provider ? { provider_name: provider, login_url: "/api/auth/oidc/login" } : null,
        },
      });
    }
    if (path === "/api/me") {
      return state.signedIn
        ? route.fulfill({ json: { user, csrf_token: CSRF_TOKEN } })
        : route.fulfill(problem(401, "not_authenticated"));
    }
    if (path === "/api/auth/local/login") {
      const result = options.loginResult ?? "ok";
      if (result === "wrong") return route.fulfill(problem(401, "invalid_credentials"));
      if (result === "limited")
        return route.fulfill(problem(429, "rate_limited", { "Retry-After": "30" }));
      state.signedIn = true;
      return route.fulfill({ json: { user, csrf_token: CSRF_TOKEN } });
    }
    if (path === "/api/auth/logout") {
      state.logoutCsrf = request.headers()["x-csrf-token"] ?? null;
      state.signedIn = false;
      if (options.providerLogoutUrl) {
        return route.fulfill({ json: { end_session_url: options.providerLogoutUrl } });
      }
      return route.fulfill({ status: 204 });
    }
    if (path === "/api/auth/oidc/login") {
      // The real endpoint redirects to the provider; the tests only need to know it was asked.
      return route.fulfill({ status: 200, contentType: "text/html", body: "<p>provider</p>" });
    }
    return route.fulfill(problem(404, "not_found"));
  });
  return state;
}
