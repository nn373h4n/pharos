import type { Locale } from "@/i18n/config";
import type english from "../messages/en.json";

// Makes useTranslations() and getTranslations() check keys against the English messages at
// compile time, so a typo in a key fails `pnpm typecheck`.
declare module "next-intl" {
  interface AppConfig {
    Locale: Locale;
    Messages: typeof english;
  }
}
