import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { Source } from "@/lib/sources";
import { SourceForm } from "./source-form";

const ID = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";
const onSubmit = vi.fn();
const onCancel = vi.fn();

function existing(changes: Partial<Source> = {}): Source {
  return {
    id: ID,
    type: "rss",
    name: "Industry news",
    config: { url: "https://news.example.org/feed.xml" },
    enabled: true,
    interval_seconds: 7200,
    next_fetch_at: "2026-10-30T10:00:00Z",
    last_fetched_at: null,
    last_success_at: null,
    consecutive_failures: 0,
    last_error_code: null,
    created_by: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...changes,
  };
}

function show(props: Partial<React.ComponentProps<typeof SourceForm>> = {}) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <SourceForm
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

describe("adding a source", () => {
  it("offers the three types, a name, the settings of the type, how often, and switching on", () => {
    show();
    const types = screen.getByLabelText(en.Sources.form.type) as HTMLSelectElement;
    expect([...types.options].map((option) => option.textContent)).toEqual([
      en.Sources.types.rss,
      en.Sources.types.github,
      en.Sources.types.hackernews,
    ]);
    expect(types.disabled).toBe(false);
    expect(screen.getByLabelText(en.Sources.form.name)).toBeDefined();
    expect(screen.getByLabelText(en.Sources.form.url)).toBeDefined();
    expect(screen.queryByLabelText(en.Sources.form.repo)).toBeNull();
    const interval = screen.getByLabelText(en.Sources.form.interval) as HTMLSelectElement;
    expect(interval.value).toBe("3600");
    expect(interval.options).toHaveLength(9);
    const enabled = screen.getByLabelText(en.Sources.form.enabledNow) as HTMLInputElement;
    expect(enabled.checked).toBe(false);
  });

  it("sends a feed", () => {
    show();
    type(en.Sources.form.name, "  Industry news ");
    type(en.Sources.form.url, " https://news.example.org/feed.xml ");
    fireEvent.change(screen.getByLabelText(en.Sources.form.interval), { target: { value: "900" } });
    fireEvent.click(screen.getByLabelText(en.Sources.form.enabledNow));
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      type: "rss",
      name: "Industry news",
      config: { url: "https://news.example.org/feed.xml" },
      interval: 900,
      enabled: true,
    });
  });

  it("changes the fields with the type and forgets what was typed for the old one", () => {
    show();
    type(en.Sources.form.url, "https://a.example/feed");
    fireEvent.change(screen.getByLabelText(en.Sources.form.type), { target: { value: "github" } });
    expect(screen.queryByLabelText(en.Sources.form.url)).toBeNull();
    type(en.Sources.form.name, "Widget");
    type(en.Sources.form.repo, "acme/widget");
    submit();
    expect(onSubmit).toHaveBeenCalledWith({
      type: "github",
      name: "Widget",
      config: { repo: "acme/widget" },
      interval: 3600,
      enabled: false,
    });
    fireEvent.change(screen.getByLabelText(en.Sources.form.type), { target: { value: "rss" } });
    expect((screen.getByLabelText(en.Sources.form.url) as HTMLInputElement).value).toBe("");
  });

  it("sends a search with its fewest points, or without", () => {
    show();
    fireEvent.change(screen.getByLabelText(en.Sources.form.type), {
      target: { value: "hackernews" },
    });
    type(en.Sources.form.name, "SAP on HN");
    type(en.Sources.form.query, "sap");
    submit();
    expect(onSubmit).toHaveBeenLastCalledWith(
      expect.objectContaining({ config: { query: "sap" } }),
    );
    type(en.Sources.form.points, "40");
    submit();
    expect(onSubmit).toHaveBeenLastCalledWith(
      expect.objectContaining({ config: { query: "sap", min_points: 40 } }),
    );
  });

  it.each([
    ["", "", "nameMissing"],
    ["N", "", "urlRequired"],
  ])("does not ask when the name is %j and the address %j", (name, url, key) => {
    show();
    type(en.Sources.form.name, name);
    type(en.Sources.form.url, url);
    submit();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain(en.Sources.form.problems);
    expect(document.body.textContent).toContain(
      en.Sources.fieldErrors[key as "nameMissing" | "urlRequired"],
    );
  });

  it.each([
    ["github", en.Sources.form.repo, "", "repoInvalid"],
    ["hackernews", en.Sources.form.query, "", "queryInvalid"],
  ])("does not ask for a %s without its settings", (kind, label, value, key) => {
    show();
    fireEvent.change(screen.getByLabelText(en.Sources.form.type), { target: { value: kind } });
    type(en.Sources.form.name, "N");
    type(label, value);
    submit();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain(
      en.Sources.fieldErrors[key as "repoInvalid" | "queryInvalid"],
    );
  });

  it.each(["abc", "-1", "1.5", "99999", "10001"])("refuses %j points before asking", (points) => {
    show();
    fireEvent.change(screen.getByLabelText(en.Sources.form.type), {
      target: { value: "hackernews" },
    });
    type(en.Sources.form.name, "N");
    type(en.Sources.form.query, "sap");
    type(en.Sources.form.points, points);
    submit();
    expect(onSubmit).not.toHaveBeenCalled();
    const field = screen.getByLabelText(en.Sources.form.points);
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(
      document.getElementById(field.getAttribute("aria-describedby")!.split(" ").pop()!)
        ?.textContent,
    ).toBe(en.Sources.fieldErrors.pointsInvalid);
  });

  it("puts focus on the summary of what is wrong", () => {
    show();
    submit();
    expect(document.activeElement).toBe(screen.getByRole("alert"));
  });
});

describe("what the backend said", () => {
  it("is shown at the field, tied to it, and the summary gets focus", () => {
    show({ problems: { config: "urlNotAllowed", name: "nameInvalid" } });
    const url = screen.getByLabelText(en.Sources.form.url);
    expect(url.getAttribute("aria-invalid")).toBe("true");
    const described = url.getAttribute("aria-describedby")!.split(" ");
    const texts = described.map((id) => document.getElementById(id)?.textContent);
    expect(texts).toContain(en.Sources.fieldErrors.urlNotAllowed);
    expect(screen.getByLabelText(en.Sources.form.name).getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("alert"));
  });

  it("is told for the whole form when it names no field", () => {
    show({ requestProblem: "sourceConflict" });
    expect(screen.getByRole("alert").textContent).toContain(en.Problems.sourceConflict);
  });

  it("shows nothing when nothing is wrong", () => {
    show();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByLabelText(en.Sources.form.name).getAttribute("aria-invalid")).toBeNull();
  });
});

describe("changing a source", () => {
  it("starts from what the source has and keeps its type", () => {
    show({ source: existing() });
    const types = screen.getByLabelText(en.Sources.form.type) as HTMLSelectElement;
    expect(types.disabled).toBe(true);
    expect(types.value).toBe("rss");
    expect(document.body.textContent).toContain(en.Sources.form.typeFixed);
    expect((screen.getByLabelText(en.Sources.form.name) as HTMLInputElement).value).toBe(
      "Industry news",
    );
    expect((screen.getByLabelText(en.Sources.form.url) as HTMLInputElement).value).toBe(
      "https://news.example.org/feed.xml",
    );
    expect((screen.getByLabelText(en.Sources.form.interval) as HTMLSelectElement).value).toBe(
      "7200",
    );
    expect(screen.queryByLabelText(en.Sources.form.enabledNow)).toBeNull();
    expect(screen.getByRole("heading", { name: en.Sources.form.editTitle })).toBeDefined();
  });

  it("sends the changes", () => {
    show({ source: existing() });
    type(en.Sources.form.name, "Renamed");
    submit();
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ type: "rss", name: "Renamed", interval: 7200 }),
    );
  });
});

describe("buttons", () => {
  it("cancel", () => {
    show();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.cancel }));
    expect(onCancel).toHaveBeenCalled();
  });
  it("wait while a request is on its way", () => {
    show({ busy: true });
    expect(
      (screen.getByRole("button", { name: en.Sources.form.saving }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: en.Sources.form.cancel }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
  it("show the add title for a new source", () => {
    show();
    expect(screen.getByRole("heading", { name: en.Sources.form.addTitle })).toBeDefined();
  });
});
