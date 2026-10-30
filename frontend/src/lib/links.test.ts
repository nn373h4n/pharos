import { describe, expect, it } from "vitest";
import { hostOf, safeHref } from "@/lib/links";

describe("safeHref", () => {
  it.each([
    ["https://news.example.org/a?b=1#c", "https://news.example.org/a?b=1#c"],
    ["http://news.example.org", "http://news.example.org/"],
    ["HTTPS://News.Example.org:8443/x", "https://news.example.org:8443/x"],
    ["https://xn--e1afmkfd.example/", "https://xn--e1afmkfd.example/"],
  ])("accepts %s", (value, expected) => {
    expect(safeHref(value)).toBe(expected);
  });

  it.each([
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "ftp://example.org/x",
    "vbscript:x",
    "blob:https://example.org/uuid",
    "//example.org/x",
    "/relative/path",
    "example.org/x",
    "",
    " https://example.org/",
    "https://example.org/ x",
    "https://example.org/\nx",
    "https://example.org/\u0000x",
    "https://example.org/​x".replace("​", "\u0085"),
    "https://example.org/\u00a0x",
    "https://example.org/\u2028x",
    "https://example.org/\u3000x",
    "https://example.org/\ud800x",
    "https://example.org/<b>",
    'https://example.org/"x',
    "https://example.org\\@evil.example/",
    "https://user:pass@example.org/",
    "https://user@example.org/",
    "https://",
    "https:///x",
    `https://example.org/${"a".repeat(2100)}`,
  ])("refuses %j", (value) => {
    expect(safeHref(value)).toBeNull();
  });

  it.each([null, undefined, 5, {}, [], true])("refuses a value that is not text: %j", (value) => {
    expect(safeHref(value)).toBeNull();
  });
});

describe("hostOf", () => {
  it("is the host of the link, with its port", () => {
    expect(hostOf("https://news.example.org:8443/a")).toBe("news.example.org:8443");
    expect(hostOf("https://news.example.org/a")).toBe("news.example.org");
    expect(hostOf("http://localhost:6379/")).toBe("localhost:6379");
  });
});
