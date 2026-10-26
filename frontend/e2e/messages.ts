import { readFileSync } from "node:fs";

type Messages = typeof import("../messages/en.json");

// Read with fs: importing JSON from an ES module needs an import attribute under Node.
function load(locale: string): Messages {
  const file = new URL(`../messages/${locale}.json`, import.meta.url);
  return JSON.parse(readFileSync(file, "utf8")) as Messages;
}

export const en = load("en");
export const az = load("az");
export const tr = load("tr");
export const ru = load("ru");
