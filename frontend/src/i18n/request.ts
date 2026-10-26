import { getRequestConfig } from "next-intl/server";
import english from "../../messages/en.json";
import { FALLBACK_LOCALE, isLocale, withFallback } from "./config";
import { routing } from "./routing";

export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale;
  const locale = isLocale(requested) ? requested : routing.defaultLocale;
  const translated =
    locale === FALLBACK_LOCALE ? english : (await import(`../../messages/${locale}.json`)).default;
  return { locale, messages: withFallback(english, translated) };
});
