import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { getAuthConfig } from "@/lib/api";

const hardNavigate = vi.fn();
vi.mock("@/lib/navigate", () => ({ hardNavigate: (url: string) => hardNavigate(url) }));

const { SessionProvider, useSession } = await import("./session-provider");

const fetchMock = vi.fn<typeof fetch>();
const session = {
  user: { id: "u1", display_name: "Ana", role: "member" },
  csrf_token: "csrf-1",
};
const outcome: { done: boolean | null } = { done: null };

function Probe() {
  const { state, signOut } = useSession();
  return (
    <>
      <p data-testid="status">{state.status}</p>
      <button
        type="button"
        onClick={async () => {
          outcome.done = await signOut();
        }}
      >
        leave
      </button>
    </>
  );
}

function json(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), { status });
}

function mount() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <SessionProvider>
        <Probe />
      </SessionProvider>
    </NextIntlClientProvider>,
  );
}

const status = () => screen.getByTestId("status").textContent;

async function leave() {
  outcome.done = null;
  fireEvent.click(screen.getByRole("button", { name: "leave" }));
  await waitFor(() => expect(outcome.done).not.toBeNull());
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  hardNavigate.mockReset();
  vi.unstubAllGlobals();
});

describe("SessionProvider", () => {
  it("starts by asking who is signed in", async () => {
    fetchMock.mockResolvedValue(json(200, session));
    mount();
    expect(status()).toBe("loading");
    await waitFor(() => expect(status()).toBe("authenticated"));
  });

  it("knows an anonymous visitor", async () => {
    fetchMock.mockResolvedValue(json(401, { code: "not_authenticated" }));
    mount();
    await waitFor(() => expect(status()).toBe("anonymous"));
  });

  it("does not take a broken backend for 'signed out'", async () => {
    fetchMock.mockResolvedValue(json(503, { code: "service_unavailable" }));
    mount();
    await waitFor(() => expect(status()).toBe("error"));
  });

  it("does not take an unreachable backend for 'signed out'", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    mount();
    await waitFor(() => expect(status()).toBe("error"));
  });

  it("becomes anonymous when a later call is answered with 401", async () => {
    fetchMock.mockResolvedValueOnce(json(200, session));
    mount();
    await waitFor(() => expect(status()).toBe("authenticated"));
    fetchMock.mockResolvedValueOnce(json(401, { code: "not_authenticated" }));
    await act(async () => {
      await getAuthConfig().catch(() => undefined);
    });
    expect(status()).toBe("anonymous");
  });

  it("signs out with the CSRF token and then leaves for the sign-in page", async () => {
    fetchMock.mockResolvedValueOnce(json(200, session));
    mount();
    await waitFor(() => expect(status()).toBe("authenticated"));
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await leave();
    expect(outcome.done).toBe(true);
    const [url, init] = fetchMock.mock.calls[1]!;
    expect(url).toBe("/api/auth/logout");
    expect((init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-1");
    expect(hardNavigate).toHaveBeenCalledWith("/en/login");
    // Left as it is until the page is replaced: "anonymous" here would let the gate start a soft
    // navigation that races the full page load (and the provider's sign-out page).
    expect(status()).toBe("authenticated");
  });

  it("goes on to the provider's sign-out page after a single sign-on session", async () => {
    fetchMock.mockResolvedValueOnce(json(200, session));
    mount();
    await waitFor(() => expect(status()).toBe("authenticated"));
    fetchMock.mockResolvedValueOnce(json(200, { end_session_url: "https://login.example/out" }));
    await leave();
    expect(hardNavigate).toHaveBeenCalledWith("https://login.example/out");
    expect(status()).toBe("authenticated");
  });

  it("still signs out when the answer of a successful sign-out cannot be read", async () => {
    fetchMock.mockResolvedValueOnce(json(200, session));
    mount();
    await waitFor(() => expect(status()).toBe("authenticated"));
    fetchMock.mockResolvedValueOnce(new Response("not json", { status: 200 }));
    await leave();
    expect(outcome.done).toBe(true);
    expect(hardNavigate).toHaveBeenCalledWith("/en/login");
  });

  it.each([
    {},
    { user: {}, csrf_token: "t" },
    { user: { display_name: "A", role: "root" }, csrf_token: "t" },
    { user: { display_name: "A", role: "member" } },
    "text",
    null,
  ])("does not take %j for a session", async (body) => {
    fetchMock.mockResolvedValue(json(200, body));
    mount();
    await waitFor(() => expect(status()).toBe("error"));
  });

  it("stays signed in, and says so, when signing out failed", async () => {
    fetchMock.mockResolvedValueOnce(json(200, session));
    mount();
    await waitFor(() => expect(status()).toBe("authenticated"));
    fetchMock.mockResolvedValueOnce(json(503, { code: "service_unavailable" }));
    await leave();
    expect(outcome.done).toBe(false);
    expect(status()).toBe("authenticated");
    expect(hardNavigate).not.toHaveBeenCalled();
  });
});
