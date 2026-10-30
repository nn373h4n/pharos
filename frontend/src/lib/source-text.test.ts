import { describe, expect, it } from "vitest";
import {
  ERROR_WORDS,
  OUTCOMES,
  errorKey,
  fieldKey,
  fieldOf,
  outcomeKey,
  problemsByField,
} from "@/lib/source-text";
import en from "../../messages/en.json";

const config = (type: string) => ({ loc: ["body", "config"], type });

describe("outcomeKey and errorKey", () => {
  it.each(OUTCOMES)("knows the outcome %s", (word) => {
    expect(outcomeKey(word)).toBe(word);
  });
  it.each(ERROR_WORDS)("knows the problem %s", (word) => {
    expect(errorKey(word)).toBe(word);
  });
  it.each([null, "", "<b>x</b>", "OK", "unknown_thing", "constructor", "__proto__"])(
    "calls %j something else",
    (word) => {
      expect(outcomeKey(word)).toBe("other");
      expect(errorKey(word ?? "")).toBe("other");
    },
  );
  it("has a message for every word", () => {
    for (const word of OUTCOMES) expect(en.Sources.outcome).toHaveProperty(word);
    for (const word of ERROR_WORDS) expect(en.Sources.lastError).toHaveProperty(word);
    expect(en.Sources.outcome).toHaveProperty("other");
    expect(en.Sources.lastError).toHaveProperty("other");
  });
});

describe("which part of the form a problem is about", () => {
  it.each([
    [["body", "name"], "name"],
    [["body", "config"], "config"],
    [["body", "interval_seconds"], "interval"],
    [["body", "type"], "form"],
    [["body", "enabled"], "form"],
    [["query", "limit"], "form"],
    [[], "form"],
  ])("%j is %s", (loc, field) => {
    expect(fieldOf({ loc, type: "x" })).toBe(field);
  });
});

describe("fieldKey", () => {
  it.each([
    ["url_required", "urlRequired"],
    ["url_invalid", "urlInvalid"],
    ["url_not_allowed", "urlNotAllowed"],
    ["repo_required", "repoInvalid"],
    ["repo_invalid", "repoInvalid"],
    ["query_required", "queryInvalid"],
    ["query_invalid", "queryInvalid"],
    ["min_points_invalid", "pointsInvalid"],
    ["unknown_setting", "configInvalid"],
    ["unknown_type", "configInvalid"],
    ["something_new", "configInvalid"],
  ])("%s about the settings is %s", (type, key) => {
    expect(fieldKey(config(type))).toBe(key);
  });
  it.each([
    ["invalid_text", "nameInvalid"],
    ["string_too_long", "nameTooLong"],
    ["string_too_short", "nameMissing"],
    ["missing", "nameMissing"],
  ])("%s about the name is %s", (type, key) => {
    expect(fieldKey({ loc: ["body", "name"], type })).toBe(key);
  });
  it("is about the interval for the interval", () => {
    expect(fieldKey({ loc: ["body", "interval_seconds"], type: "less_than_equal" })).toBe(
      "intervalInvalid",
    );
  });
  it("has a message for every key", () => {
    for (const key of Object.keys(en.Sources.fieldErrors)) expect(key).toBeTruthy();
    for (const type of ["url_required", "something_new"]) {
      expect(en.Sources.fieldErrors).toHaveProperty(fieldKey(config(type)));
    }
  });
});

describe("problemsByField", () => {
  it("keeps the first problem of each part", () => {
    expect(
      problemsByField([
        config("url_invalid"),
        config("url_required"),
        { loc: ["body", "name"], type: "missing" },
      ]),
    ).toEqual({ config: "urlInvalid", name: "nameMissing" });
  });
  it("is empty when nothing is wrong", () => {
    expect(problemsByField([])).toEqual({});
  });
});
