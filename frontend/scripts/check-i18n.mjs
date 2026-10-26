// Fails the build when a locale's messages drift from the English source (ADR-0008).
// Run by `pnpm lint` and, through the `prebuild` script, before every `pnpm build`.

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SOURCE_LOCALE = "en";

/** Flattens nested messages to "A.b.c" -> text. Anything that is not an object or a string is an error. */
export function flatten(messages, prefix = "") {
  const result = new Map();
  for (const [key, value] of Object.entries(messages)) {
    const fullKey = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") {
      result.set(fullKey, value);
    } else if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      for (const [nestedKey, nestedValue] of flatten(value, fullKey)) {
        result.set(nestedKey, nestedValue);
      }
    } else {
      throw new Error(`${fullKey}: messages must be strings or objects`);
    }
  }
  return result;
}

/** ICU argument names used by a message, for example "{count, plural, ...}" -> ["count"]. */
export function placeholders(message) {
  return [
    ...new Set([...message.matchAll(/\{\s*([A-Za-z_]\w*)/g)].map((match) => match[1])),
  ].sort();
}

/** Returns a list of human-readable problems; an empty list means every locale is complete. */
export function findProblems(messagesByLocale) {
  const problems = [];
  const source = messagesByLocale[SOURCE_LOCALE];
  if (!source) {
    return [`${SOURCE_LOCALE}: source messages are missing`];
  }
  const sourceKeys = flatten(source);
  for (const [key, text] of sourceKeys) {
    if (text.trim() === "") problems.push(`${SOURCE_LOCALE}: ${key} is empty`);
  }

  for (const [locale, messages] of Object.entries(messagesByLocale)) {
    if (locale === SOURCE_LOCALE) continue;
    const keys = flatten(messages);
    for (const [key, sourceText] of sourceKeys) {
      const text = keys.get(key);
      if (text === undefined) {
        problems.push(`${locale}: missing key ${key}`);
      } else if (text.trim() === "") {
        problems.push(`${locale}: ${key} is empty`);
      } else if (placeholders(text).join() !== placeholders(sourceText).join()) {
        problems.push(`${locale}: ${key} uses different placeholders than ${SOURCE_LOCALE}`);
      }
    }
    for (const key of keys.keys()) {
      if (!sourceKeys.has(key))
        problems.push(`${locale}: ${key} does not exist in ${SOURCE_LOCALE}`);
    }
  }
  return problems.sort();
}

export function loadMessages(directory) {
  const messages = {};
  for (const file of readdirSync(directory).filter((name) => name.endsWith(".json"))) {
    messages[path.basename(file, ".json")] = JSON.parse(
      readFileSync(path.join(directory, file), "utf8"),
    );
  }
  return messages;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const directory = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "messages");
  const problems = findProblems(loadMessages(directory));
  if (problems.length > 0) {
    console.error(`i18n check failed:\n${problems.map((problem) => `  - ${problem}`).join("\n")}`);
    process.exit(1);
  }
  console.log(`i18n check passed (${Object.keys(loadMessages(directory)).join(", ")})`);
}
