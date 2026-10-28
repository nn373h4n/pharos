import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { AuthConfig, Session } from "@/lib/api";

const search = new URLSearchParams();
const replace = vi.fn();
const hardNavigate = vi.fn();
const getAuthConfig = vi.fn<() => Promise<AuthConfig>>();
const localLogin = vi.fn<(username: string, password: string) => Promise<Session>>();
const setSession = vi.fn();
const signOut = vi.fn<() => Promise<boolean>>();
let sessionState: unknown = { status: "anonymous" };

vi.mock("next/navigation", () => ({ useSearchParams: () => search }));
vi.mock("@/i18n/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/lib/navigate", () => ({ hardNavigate: (url: string) => hardNavigate(url) }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  getAuthConfig: () => getAuthConfig(),
  localLogin: (username: string, password: string) => localLogin(username, password),
}));
vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({ state: sessionState, setSession, signOut }),
}));

const { ApiProblem } = await import("@/lib/api");
const { LoginPanel } = await import("./login-panel");
const { AppGate } = await import("./app-gate");
const { UserMenu } = await import("./user-menu");

const user = {
  id: "u1",
  display_name: "Aysel Məmmədova",
  email: null,
  role: "editor" as const,
  locale: null,
  department: null,
  last_login_at: null,
  onboarding_completed_at: null,
};

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function show(element: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      {element}
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  for (const key of [...search.keys()]) search.delete(key);
  sessionState = { status: "anonymous" };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("LoginPanel", () => {
  const both: AuthConfig = {
    local_login: "on",
    oidc: { provider_name: "Contoso", login_url: "/api/auth/oidc/login" },
  };

  it("offers single sign-on first and carries the target page along", async () => {
    getAuthConfig.mockResolvedValue(both);
    search.set("next", "/az/library?tab=1");
    show(<LoginPanel />);
    const link = await screen.findByRole("link", { name: "Sign in with Contoso" });
    expect(link.getAttribute("href")).toBe("/api/auth/oidc/login?next=%2Faz%2Flibrary%3Ftab%3D1");
    expect(screen.getByLabelText("Username")).toBeTruthy();
  });

  it("never passes on a target that is not a page of this site", async () => {
    getAuthConfig.mockResolvedValue(both);
    search.set("next", "//evil.test/x");
    show(<LoginPanel />);
    const link = await screen.findByRole("link", { name: "Sign in with Contoso" });
    expect(link.getAttribute("href")).toBe("/api/auth/oidc/login?next=%2F");
  });

  it("hides the password form when password sign-in is off", async () => {
    getAuthConfig.mockResolvedValue({ ...both, local_login: "off" });
    show(<LoginPanel />);
    await screen.findByRole("link", { name: "Sign in with Contoso" });
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("says that password sign-in is for administrators in break-glass mode", async () => {
    getAuthConfig.mockResolvedValue({ local_login: "break-glass", oidc: null });
    show(<LoginPanel />);
    expect(await screen.findByText("Password sign-in is only for administrators.")).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("says so when nothing is set up", async () => {
    getAuthConfig.mockResolvedValue({ local_login: "off", oidc: null });
    show(<LoginPanel />);
    expect(await screen.findByText(/Sign-in is not set up/)).toBeTruthy();
  });

  it("tells the person when the options cannot be loaded", async () => {
    getAuthConfig.mockRejectedValue(new ApiProblem(0, "network_error"));
    show(<LoginPanel />);
    expect((await screen.findByRole("alert")).textContent).toContain("could not be loaded");
  });

  it("signs in with a password and goes to the target page", async () => {
    getAuthConfig.mockResolvedValue({ local_login: "on", oidc: null });
    localLogin.mockResolvedValue({ user, csrf_token: "t" });
    search.set("next", "/en/library");
    show(<LoginPanel />);
    await screen.findByLabelText("Username");
    fill("Username", "ana");
    fill("Password", "correct horse");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(hardNavigate).toHaveBeenCalledWith("/en/library"));
    expect(localLogin).toHaveBeenCalledWith("ana", "correct horse");
    expect(setSession).toHaveBeenCalledWith({ user, csrf_token: "t" });
  });

  it("shows one message for wrong credentials and clears only the password", async () => {
    getAuthConfig.mockResolvedValue({ local_login: "on", oidc: null });
    localLogin.mockRejectedValue(new ApiProblem(401, "invalid_credentials"));
    show(<LoginPanel />);
    await screen.findByLabelText("Username");
    fill("Username", "ana");
    fill("Password", "nope");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "The username or the password is wrong.",
    );
    expect((screen.getByLabelText("Username") as HTMLInputElement).value).toBe("ana");
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
    expect(screen.getByLabelText("Password").getAttribute("aria-invalid")).toBe("true");
    expect(hardNavigate).not.toHaveBeenCalled();
  });

  it("tells how long to wait when there were too many attempts", async () => {
    getAuthConfig.mockResolvedValue({ local_login: "on", oidc: null });
    localLogin.mockRejectedValue(new ApiProblem(429, "rate_limited", 17));
    show(<LoginPanel />);
    await screen.findByLabelText("Username");
    fill("Username", "ana");
    fill("Password", "x");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Too many attempts. Try again in 17 seconds.",
    );
  });

  it.each([
    [new ApiProblem(503, "service_unavailable"), "The service is not available right now"],
    [new ApiProblem(0, "network_error"), "The service is not available right now"],
    [new ApiProblem(422, "validation_failed"), "Something went wrong"],
  ])("explains a failure of the sign-in call (%#)", async (problem, text) => {
    getAuthConfig.mockResolvedValue({ local_login: "on", oidc: null });
    localLogin.mockRejectedValue(problem);
    show(<LoginPanel />);
    await screen.findByLabelText("Username");
    fill("Username", "ana");
    fill("Password", "x");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toContain(text);
  });

  it.each([
    ["sso_failed", "Single sign-on did not work"],
    ["sso_denied", "Sign-in was cancelled."],
    ["sso_unavailable", "cannot be reached"],
    ["account_disabled", "This account is disabled"],
    ["something_new", "Something went wrong"],
  ])("explains the error %s that single sign-on left in the address", async (code, text) => {
    getAuthConfig.mockResolvedValue(both);
    search.set("error", code);
    show(<LoginPanel />);
    expect((await screen.findByRole("alert")).textContent).toContain(text);
  });

  it("sends someone who is already signed in on to the target page", async () => {
    getAuthConfig.mockResolvedValue(both);
    sessionState = { status: "authenticated", session: { user, csrf_token: "t" } };
    search.set("next", "/en/library");
    show(<LoginPanel />);
    await waitFor(() => expect(hardNavigate).toHaveBeenCalledWith("/en/library"));
  });
});

describe("AppGate", () => {
  it("shows the page to a signed-in person", () => {
    sessionState = { status: "authenticated", session: { user, csrf_token: "t" } };
    show(
      <AppGate>
        <p>secret page</p>
      </AppGate>,
    );
    expect(screen.getByText("secret page")).toBeTruthy();
    expect(replace).not.toHaveBeenCalled();
  });

  it("shows only a status while the session is being checked", () => {
    sessionState = { status: "loading" };
    show(
      <AppGate>
        <p>secret page</p>
      </AppGate>,
    );
    expect(screen.queryByText("secret page")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("Loading…");
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends an anonymous visitor to sign in and back to the same page afterwards", async () => {
    window.history.pushState({}, "", "/az/library?tab=1");
    sessionState = { status: "anonymous" };
    show(
      <AppGate>
        <p>secret page</p>
      </AppGate>,
    );
    expect(screen.queryByText("secret page")).toBeNull();
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith({
        pathname: "/login",
        query: { next: "/az/library?tab=1" },
      }),
    );
  });

  it("does not take a failed check for 'signed out'", () => {
    sessionState = { status: "error" };
    show(
      <AppGate>
        <p>secret page</p>
      </AppGate>,
    );
    expect(screen.queryByText("secret page")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("not available");
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("UserMenu", () => {
  it("shows nothing without a session", () => {
    const { container } = show(<UserMenu />);
    expect(container.textContent).toBe("");
  });

  it("shows who is signed in and signs out", () => {
    sessionState = { status: "authenticated", session: { user, csrf_token: "t" } };
    signOut.mockResolvedValue(true);
    show(<UserMenu />);
    const group = screen.getByRole("group", { name: "Account" });
    expect(group.textContent).toContain("Aysel Məmmədova");
    expect(group.textContent).toContain("Editor");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(signOut).toHaveBeenCalledTimes(1);
  });

  it("says so when signing out failed, and lets the person try again", async () => {
    sessionState = { status: "authenticated", session: { user, csrf_token: "t" } };
    signOut.mockResolvedValue(false);
    show(<UserMenu />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect((await screen.findByRole("alert")).textContent).toContain("did not work");
    expect((screen.getByRole("button", { name: "Sign out" }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });
});
