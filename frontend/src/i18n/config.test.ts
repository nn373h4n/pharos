import { describe, expect, it } from "vitest";
import { isLocale, locales, resolveDefaultLocale, withFallback } from "./config";

describe("resolveDefaultLocale", () => {
  it.each(locales)("accepts the supported locale %s", (locale) => {
    expect(resolveDefaultLocale(locale)).toBe(locale);
  });

  it.each([undefined, "", "de", "EN", "en-US", "az ", "__proto__"])(
    "falls back to English for %j",
    (value) => {
      expect(resolveDefaultLocale(value)).toBe("en");
    },
  );
});

describe("isLocale", () => {
  it("accepts only supported locales", () => {
    expect(isLocale("tr")).toBe(true);
    expect(isLocale("de")).toBe(false);
    expect(isLocale(undefined)).toBe(false);
    expect(isLocale(42)).toBe(false);
  });
});

describe("withFallback", () => {
  const english = { A: { one: "one", two: "two" }, B: "bee" };

  it("fills missing keys, also inside nested sections", () => {
    expect(withFallback(english, { A: { one: "bir" } })).toEqual({
      A: { one: "bir", two: "two" },
      B: "bee",
    });
  });

  it("keeps every translated value", () => {
    const translated = { A: { one: "bir", two: "iki" }, B: "arı" };
    expect(withFallback(english, translated)).toEqual(translated);
  });

  it("does not modify its inputs", () => {
    const translated = { A: { one: "bir" } };
    withFallback(english, translated);
    expect(english).toEqual({ A: { one: "one", two: "two" }, B: "bee" });
    expect(translated).toEqual({ A: { one: "bir" } });
  });
});
