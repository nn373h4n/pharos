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
  /** False: the person still has to answer the onboarding questions. Default: done. */
  onboarded?: boolean;
  /** Answers already saved, for someone who left onboarding halfway. */
  profile?: Partial<Profile>;
};

export const CSRF_TOKEN = "csrf-e2e";
export const USER_NAME = "Test User";

type Profile = {
  profession_id: string | null;
  ai_level: string | null;
  interests: string[];
  department_id: string | null;
  locale: string | null;
};

export type MockState = {
  signedIn: boolean;
  onboarded: boolean;
  profile: Profile;
  /** Every call the page made, as "METHOD /path". */
  calls: string[];
  /** The bodies of the profile saves, in order. */
  saves: Record<string, unknown>[];
  /** The X-CSRF-Token header of the last sign-out call. */
  logoutCsrf: string | null;
  /** The X-CSRF-Token headers of the profile saves and of the completion. */
  profileCsrf: (string | null)[];
};

export const PROFESSIONS = [
  {
    id: "10000000-0000-4000-8000-000000000001",
    code: "accountant",
    names: { en: "Accountant", az: "Mühasib", tr: "Muhasebeci", ru: "Бухгалтер" },
  },
  {
    id: "10000000-0000-4000-8000-000000000002",
    code: "hr_specialist",
    names: { en: "HR specialist", az: "HR mütəxəssisi", tr: "İK uzmanı", ru: "Специалист по HR" },
  },
];
export const DEPARTMENTS = [{ id: "20000000-0000-4000-8000-000000000001", name: "Finance" }];
export const OPTIONS = {
  ai_levels: ["beginner", "intermediate", "advanced"],
  interests: [
    "writing",
    "summarizing",
    "translation",
    "data_analysis",
    "reporting",
    "research",
    "automation",
    "coding",
    "meetings",
    "planning",
    "customer_support",
    "learning",
  ],
  locales: ["en", "az", "tr", "ru"],
};
const PROFILE_FIELDS = ["profession_id", "ai_level", "interests", "department_id", "locale"];

export async function mockApi(
  target: Page | BrowserContext,
  options: MockOptions = {},
): Promise<MockState> {
  const state: MockState = {
    signedIn: options.signedIn ?? false,
    onboarded: options.onboarded ?? true,
    profile: {
      profession_id: null,
      ai_level: null,
      interests: [],
      department_id: null,
      locale: null,
      ...options.profile,
    },
    calls: [],
    saves: [],
    logoutCsrf: null,
    profileCsrf: [],
  };
  const user = () => ({
    id: "00000000-0000-4000-8000-000000000001",
    display_name: USER_NAME,
    email: null,
    role: options.role ?? "member",
    locale: state.profile.locale,
    department: null,
    last_login_at: null,
    onboarding_completed_at: state.onboarded ? "2026-10-28T09:00:00Z" : null,
  });
  const profileOut = () => ({
    ...state.profile,
    effective_locale: state.profile.locale ?? "en",
    onboarding_started_at: null,
    onboarding_completed_at: state.onboarded ? "2026-10-28T09:00:00Z" : null,
  });
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
        ? route.fulfill({ json: { user: user(), csrf_token: CSRF_TOKEN } })
        : route.fulfill(problem(401, "not_authenticated"));
    }
    if (path === "/api/auth/local/login") {
      const result = options.loginResult ?? "ok";
      if (result === "wrong") return route.fulfill(problem(401, "invalid_credentials"));
      if (result === "limited")
        return route.fulfill(problem(429, "rate_limited", { "Retry-After": "30" }));
      state.signedIn = true;
      return route.fulfill({ json: { user: user(), csrf_token: CSRF_TOKEN } });
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
    const changes = request.method() !== "GET";
    if (changes && request.headers()["x-csrf-token"] !== CSRF_TOKEN) {
      return route.fulfill(problem(403, "csrf_failed"));
    }
    const needsSession =
      path.startsWith("/api/me/") || path === "/api/professions" || path === "/api/departments";
    if (!state.signedIn && needsSession) {
      return route.fulfill(problem(401, "not_authenticated"));
    }
    if (path === "/api/me/profile" && request.method() === "GET") {
      return route.fulfill({ json: profileOut() });
    }
    if (path === "/api/me/profile" && request.method() === "PUT") {
      const body = request.postDataJSON() as Record<string, unknown>;
      if (!Object.keys(body).every((key) => PROFILE_FIELDS.includes(key))) {
        return route.fulfill(problem(422, "validation_failed"));
      }
      state.profileCsrf.push(request.headers()["x-csrf-token"] ?? null);
      state.saves.push(body);
      Object.assign(state.profile, body);
      return route.fulfill({ json: profileOut() });
    }
    if (path === "/api/me/onboarding/complete") {
      state.profileCsrf.push(request.headers()["x-csrf-token"] ?? null);
      const missing = [
        !state.profile.profession_id && "profession_id",
        state.profile.interests.length === 0 && "interests",
        !state.profile.locale && "locale",
        !state.profile.ai_level && "ai_level",
      ].filter(Boolean);
      if (missing.length > 0) {
        return route.fulfill({
          status: 409,
          contentType: "application/problem+json",
          body: JSON.stringify({ code: "onboarding_incomplete", status: 409, missing }),
        });
      }
      state.onboarded = true;
      return route.fulfill({ json: profileOut() });
    }
    if (path === "/api/me/profile/options") return route.fulfill({ json: OPTIONS });
    if (path === "/api/professions") return route.fulfill({ json: PROFESSIONS });
    if (path === "/api/departments") return route.fulfill({ json: DEPARTMENTS });
    return route.fulfill(problem(404, "not_found"));
  });
  return state;
}
