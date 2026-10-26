export const locales = ["en", "az", "tr", "ru"] as const;
export type Locale = (typeof locales)[number];

/** `en` is the source language of the messages and the fallback for everything else (ADR-0008). */
export const FALLBACK_LOCALE: Locale = "en";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (locales as readonly string[]).includes(value);
}

/**
 * Each installation picks its own default language with PHAROS_DEFAULT_LOCALE. An unset or
 * unsupported value falls back to English instead of failing the whole site.
 */
export function resolveDefaultLocale(value: string | undefined): Locale {
  return isLocale(value) ? value : FALLBACK_LOCALE;
}

type Messages = { [key: string]: string | Messages };

/** Fills every key a locale lacks with the English text, so a gap never shows a raw key. */
export function withFallback(source: Messages, translated: Messages): Messages {
  const merged: Messages = { ...source };
  for (const [key, value] of Object.entries(translated)) {
    const base = source[key];
    merged[key] =
      typeof value === "object" && typeof base === "object" ? withFallback(base, value) : value;
  }
  return merged;
}
