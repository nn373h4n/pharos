import { describe, expect, it } from "vitest";
import { formatDate } from "@/lib/format";

describe("formatDate", () => {
  it.each(["en", "az", "tr", "ru"])("writes a date in %s", (locale) => {
    const text = formatDate("2026-10-20T10:00:00Z", locale);
    expect(text).not.toBeNull();
    expect(text).toMatch(/2026/);
  });

  it("writes the same day differently in different languages", () => {
    expect(formatDate("2026-10-20T10:00:00Z", "en")).not.toBe(
      formatDate("2026-10-20T10:00:00Z", "ru"),
    );
  });

  it.each([null, undefined, "", "yesterday", "2026-13-45", "not a date"])(
    "gives nothing for %j",
    (value) => {
      expect(formatDate(value, "en")).toBeNull();
    },
  );

  it("gives nothing for a language tag the browser refuses", () => {
    expect(formatDate("2026-10-20T10:00:00Z", "not a locale!")).toBeNull();
  });
});
