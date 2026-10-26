import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { findProblems, flatten, loadMessages, placeholders } from "./check-i18n.mjs";

const complete = {
  en: { Home: { title: "Hello {name}", body: "Body" }, Common: { ok: "OK" } },
  az: { Home: { title: "Salam {name}", body: "Mətn" }, Common: { ok: "Yaxşı" } },
};

describe("flatten", () => {
  it("turns nested messages into dotted keys", () => {
    expect([...flatten(complete.en).keys()]).toEqual(["Home.title", "Home.body", "Common.ok"]);
  });

  it("rejects values that are neither strings nor objects", () => {
    expect(() => flatten({ A: 1 })).toThrow("A: messages must be strings or objects");
    expect(() => flatten({ A: ["x"] })).toThrow("A: messages must be strings or objects");
    expect(() => flatten({ A: null })).toThrow("A: messages must be strings or objects");
  });
});

describe("placeholders", () => {
  it("finds simple and ICU arguments once each", () => {
    expect(
      placeholders("Hi {name}, {name} has {count, plural, one {# item} other {# items}}"),
    ).toEqual(["count", "name"]);
    expect(placeholders("No arguments")).toEqual([]);
  });
});

describe("findProblems", () => {
  it("accepts complete locales", () => {
    expect(findProblems(complete)).toEqual([]);
  });

  it("reports a key that is missing from a locale", () => {
    const messages = {
      ...complete,
      az: { Home: { title: "Salam {name}" }, Common: { ok: "Yaxşı" } },
    };
    expect(findProblems(messages)).toEqual(["az: missing key Home.body"]);
  });

  it("reports a whole missing section", () => {
    const messages = { ...complete, az: { Home: complete.az.Home } };
    expect(findProblems(messages)).toEqual(["az: missing key Common.ok"]);
  });

  it("reports a key that does not exist in the English source", () => {
    const messages = { ...complete, az: { ...complete.az, Extra: { key: "x" } } };
    expect(findProblems(messages)).toEqual(["az: Extra.key does not exist in en"]);
  });

  it("reports empty and whitespace-only translations", () => {
    const messages = { ...complete, az: { Home: { title: "  ", body: "" }, Common: { ok: "x" } } };
    expect(findProblems(messages)).toEqual(["az: Home.body is empty", "az: Home.title is empty"]);
  });

  it("reports placeholders that differ from the source", () => {
    const messages = {
      ...complete,
      az: { ...complete.az, Home: { title: "Salam {ad}", body: "x" } },
    };
    expect(findProblems(messages)).toEqual(["az: Home.title uses different placeholders than en"]);
  });

  it("requires the English source", () => {
    expect(findProblems({ az: complete.az })).toEqual(["en: source messages are missing"]);
  });
});

describe("the real messages", () => {
  const directory = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "messages");
  const messages = loadMessages(directory);

  it("cover exactly the four supported languages", () => {
    expect(Object.keys(messages).sort()).toEqual(["az", "en", "ru", "tr"]);
  });

  it("are complete and consistent", () => {
    expect(findProblems(messages)).toEqual([]);
  });
});
