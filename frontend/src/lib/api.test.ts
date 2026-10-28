import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiProblem,
  getAuthConfig,
  getSession,
  localLogin,
  logout,
  onUnauthorized,
  safeNext,
} from "./api";

function answer(status: number, body?: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), { status, headers });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe("safeNext", () => {
  it.each(["/", "/az", "/az/library?tab=new", "/en/a/b#c", "/en/file.v1", "/en/a?x=../b"])(
    "keeps the page %s",
    (page) => {
      expect(safeNext(page)).toBe(page);
    },
  );

  it.each([
    null,
    undefined,
    "",
    "az/page",
    "//evil.test",
    "//evil.test/x",
    "https://evil.test",
    "javascript:alert(1)",
    "/\\evil.test",
    "/a\\b",
    "/a\nb",
    "/a\u0000b",
    "/api",
    "/api/me",
    "/api/auth/oidc/login",
    "/en/../api/me",
    "/%2e%2e/api/me",
    "/en/%2E%2E/api/auth/oidc/login",
    "/en/./library",
    "/en/library/..",
    "/api%2Fme",
    "/%E0%A4%A",
    `/${"a".repeat(600)}`,
  ])("turns %j into the start page", (value) => {
    expect(safeNext(value as string | null | undefined)).toBe("/");
  });
});

describe("requests", () => {
  it("sends same-origin requests that are never cached", async () => {
    fetchMock.mockResolvedValue(answer(200, { local_login: "on", oidc: null }));
    await getAuthConfig();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/auth/config");
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
  });

  it("returns null for 'no session' without announcing a session end", async () => {
    const listener = vi.fn();
    const stop = onUnauthorized(listener);
    fetchMock.mockResolvedValue(answer(401, { code: "not_authenticated" }));
    expect(await getSession()).toBeNull();
    expect(listener).not.toHaveBeenCalled();
    stop();
  });

  it("throws other failures of /me instead of calling them 'signed out'", async () => {
    fetchMock.mockResolvedValue(answer(503, { code: "service_unavailable" }));
    await expect(getSession()).rejects.toMatchObject({ status: 503, code: "service_unavailable" });
  });

  it("announces a 401 on any other call, so the app can send the person to sign in", async () => {
    const listener = vi.fn();
    const stop = onUnauthorized(listener);
    fetchMock.mockResolvedValue(answer(401, { code: "not_authenticated" }));
    await expect(getAuthConfig()).rejects.toBeInstanceOf(ApiProblem);
    expect(listener).toHaveBeenCalledTimes(1);
    stop();
    fetchMock.mockResolvedValue(answer(401, { code: "not_authenticated" }));
    await expect(getAuthConfig()).rejects.toBeInstanceOf(ApiProblem);
    expect(listener).toHaveBeenCalledTimes(1); // unsubscribed
  });

  it("does not take wrong credentials for an ended session", async () => {
    const listener = vi.fn();
    const stop = onUnauthorized(listener);
    fetchMock.mockResolvedValue(answer(401, { code: "invalid_credentials" }));
    await expect(localLogin("ana", "wrong")).rejects.toMatchObject({
      status: 401,
      code: "invalid_credentials",
    });
    expect(listener).not.toHaveBeenCalled();
    stop();
  });

  it("sends the credentials as JSON and nothing else", async () => {
    fetchMock.mockResolvedValue(answer(200, { user: {}, csrf_token: "t" }));
    await localLogin("ana", "pw");
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ username: "ana", password: "pw" });
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("reads the code and the wait from a refusal", async () => {
    fetchMock.mockResolvedValue(answer(429, { code: "rate_limited" }, { "Retry-After": "17" }));
    await expect(localLogin("ana", "pw")).rejects.toMatchObject({
      status: 429,
      code: "rate_limited",
      retryAfter: 17,
    });
  });

  it("survives an error page that is not JSON", async () => {
    fetchMock.mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
    await expect(getAuthConfig()).rejects.toMatchObject({ status: 502, code: "http_error" });
  });

  it("turns a network failure into status 0", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(getAuthConfig()).rejects.toMatchObject({ status: 0, code: "network_error" });
  });
});

describe("logout", () => {
  it("sends the CSRF token and has no provider to visit after a password session", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    expect(await logout("csrf-1")).toBeNull();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/auth/logout");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-1");
  });

  it("returns the provider's sign-out address after a single sign-on session", async () => {
    fetchMock.mockResolvedValue(
      answer(200, { end_session_url: "https://login.example/logout?x=1" }),
    );
    expect(await logout("csrf-1")).toBe("https://login.example/logout?x=1");
  });

  it.each(["javascript:alert(1)", "//evil.test", "/relative", 5, null])(
    "ignores an end-session address that is not http(s): %j",
    async (value) => {
      fetchMock.mockResolvedValue(answer(200, { end_session_url: value }));
      expect(await logout("csrf-1")).toBeNull();
    },
  );
});
