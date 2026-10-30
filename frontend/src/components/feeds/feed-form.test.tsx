import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { Feed } from "@/lib/feeds";
import { FeedForm } from "./feed-form";

const onSubmit = vi.fn();
const onCancel = vi.fn();

const existing: Feed = {
  id: "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b",
  name: "My reading",
  url: "https://blog.example.org/feed.xml",
  enabled: true,
  interval_seconds: 7200,
  next_fetch_at: "2026-10-30T10:00:00Z",
  last_fetched_at: null,
  last_success_at: null,
  consecutive_failures: 0,
  last_error_code: null,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
};

function show(props: Partial<React.ComponentProps<typeof FeedForm>> = {}) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <FeedForm
        busy={false}
        problems={{}}
        requestProblem={null}
        onSubmit={onSubmit}
        onCancel={onCancel}
        {...props}
      />
    </NextIntlClientProvider>,
  );
}

const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const submit = () => fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("adding a feed", () => {
  it("asks for a name, an address and how often, and follows now by default", () => {
    show();
    expect(screen.getByRole("heading", { name: en.Feeds.form.addTitle })).toBeDefined();
    const how = screen.getByLabelText(en.Sources.form.interval) as HTMLSelectElement;
    expect([...how.options].map((option) => option.value)).toEqual([
      "3600",
      "7200",
      "21600",
      "43200",
      "86400",
      "604800",
    ]);
    expect((screen.getByLabelText(en.Feeds.form.followNow) as HTMLInputElement).checked).toBe(true);
  });

  it("sends what was typed, trimmed", () => {
    show();
    type(en.Sources.form.name, "  Fresh  ");
    type(en.Sources.form.url, " https://fresh.example.org/feed.xml ");
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      name: "Fresh",
      url: "https://fresh.example.org/feed.xml",
      interval_seconds: 3600,
      enabled: true,
    });
  });

  it("does not send a blank name or address, and says which", () => {
    show();
    submit();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(en.Sources.fieldErrors.nameMissing)).toBeDefined();
    expect(screen.getByText(en.Sources.fieldErrors.urlRequired)).toBeDefined();
    expect(screen.getByRole("alert").textContent).toContain(en.Sources.form.problems);
  });

  it("shows what the backend refused at the field", () => {
    show({ problems: { config: "urlNotAllowed" } });
    expect(screen.getByLabelText(en.Sources.form.url).getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByText(en.Sources.fieldErrors.urlNotAllowed)).toBeDefined();
  });

  it("tells a problem with the request as a whole", () => {
    show({ requestProblem: "sourceLimit" });
    expect(screen.getByRole("alert").textContent).toContain(en.Problems.sourceLimit);
  });

  it("locks the buttons while it is sent", () => {
    show({ busy: true });
    expect(
      (screen.getByRole("button", { name: en.Sources.form.saving }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: en.Sources.form.cancel }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("cancels", () => {
    show();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.cancel }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe("changing a feed", () => {
  it("starts from the feed and has no switch for following", () => {
    show({ feed: existing });
    expect(screen.getByRole("heading", { name: en.Feeds.form.editTitle })).toBeDefined();
    expect((screen.getByLabelText(en.Sources.form.name) as HTMLInputElement).value).toBe(
      "My reading",
    );
    expect((screen.getByLabelText(en.Sources.form.url) as HTMLInputElement).value).toBe(
      existing.url,
    );
    expect((screen.getByLabelText(en.Sources.form.interval) as HTMLSelectElement).value).toBe(
      "7200",
    );
    expect(screen.queryByLabelText(en.Feeds.form.followNow)).toBeNull();
  });

  it("offers an interval the form does not know, so it is not changed by accident", () => {
    show({ feed: { ...existing, interval_seconds: 5400 } });
    expect((screen.getByLabelText(en.Sources.form.interval) as HTMLSelectElement).value).toBe(
      "5400",
    );
  });
});
