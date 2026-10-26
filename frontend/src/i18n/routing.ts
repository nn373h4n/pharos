import { defineRouting } from "next-intl/routing";
import { locales, resolveDefaultLocale } from "./config";

export const routing = defineRouting({
  locales,
  defaultLocale: resolveDefaultLocale(process.env.PHAROS_DEFAULT_LOCALE),
  // Predictable, shareable URLs: /en, /az, /tr, /ru.
  localePrefix: "always",
});
