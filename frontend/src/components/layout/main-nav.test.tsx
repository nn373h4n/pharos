import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import ru from "../../../messages/ru.json";

let sessionState: unknown = { status: "anonymous" };
let pathname = "/";

vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({ state: sessionState }),
}));
vi.mock("@/i18n/navigation", () => ({
  usePathname: () => pathname,
  Link: ({
    href,
    children,
    ...props
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

const { MainNav } = await import("./main-nav");

function user(onboarded: boolean, role = "member") {
  return {
    status: "authenticated",
    session: {
      csrf_token: "t",
      user: {
        id: "u1",
        display_name: "A",
        email: null,
        role,
        locale: null,
        department: null,
        last_login_at: null,
        onboarding_completed_at: onboarded ? "2026-10-28T09:00:00Z" : null,
      },
    },
  };
}

function show(
  messages: typeof en | typeof ru = en,
  locale: "en" | "ru" = "en",
): ReturnType<typeof render> {
  const content: ReactNode = <MainNav />;
  return render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      {content}
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  sessionState = { status: "anonymous" };
  pathname = "/";
});

describe("MainNav", () => {
  it.each([[{ status: "loading" }], [{ status: "anonymous" }], [{ status: "error" }]])(
    "shows nothing without a session: %j",
    (state) => {
      sessionState = state;
      const { container } = show();
      expect(container.innerHTML).toBe("");
    },
  );

  it("shows nothing while the onboarding questions are unanswered", () => {
    sessionState = user(false);
    const { container } = show();
    expect(container.innerHTML).toBe("");
  });

  it("is a labelled navigation with a link to each page and marks the current one", () => {
    sessionState = user(true);
    pathname = "/items";
    show();
    const nav = screen.getByRole("navigation", { name: en.Nav.main });
    const links = nav.querySelectorAll("a");
    expect([...links].map((link) => link.getAttribute("href"))).toEqual(["/", "/items"]);
    expect(screen.getByRole("link", { name: en.Nav.items }).getAttribute("aria-current")).toBe(
      "page",
    );
    expect(screen.getByRole("link", { name: en.Nav.home }).getAttribute("aria-current")).toBeNull();
  });

  it("marks the home page only on the home page", () => {
    sessionState = user(true);
    pathname = "/";
    show();
    expect(screen.getByRole("link", { name: en.Nav.home }).getAttribute("aria-current")).toBe(
      "page",
    );
    expect(
      screen.getByRole("link", { name: en.Nav.items }).getAttribute("aria-current"),
    ).toBeNull();
  });

  it("keeps the article page marked on a page below it", () => {
    sessionState = user(true);
    pathname = "/items/anything";
    show();
    expect(screen.getByRole("link", { name: en.Nav.items }).getAttribute("aria-current")).toBe(
      "page",
    );
  });

  it("speaks the language of the page", () => {
    sessionState = user(true);
    show(ru, "ru");
    expect(screen.getByRole("navigation", { name: ru.Nav.main })).toBeDefined();
    expect(screen.getByRole("link", { name: ru.Nav.items })).toBeDefined();
  });

  it.each([
    ["admin", true],
    ["editor", true],
    ["member", false],
  ])("shows the sources page to %s: %s", (role, seen) => {
    sessionState = user(true, role);
    show();
    const hrefs = [...screen.getByRole("navigation").querySelectorAll("a")].map((link) =>
      link.getAttribute("href"),
    );
    expect(hrefs.includes("/sources")).toBe(seen);
  });
});
