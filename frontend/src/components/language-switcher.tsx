"use client";

import { useLocale, useTranslations } from "next-intl";
import { buttonVariants } from "@/components/ui/button";
import { locales, type Locale } from "@/i18n/config";
import { Link, usePathname } from "@/i18n/navigation";
import { cn } from "@/lib/utils";

// Each language is named in itself, so someone who cannot read the current page can still find theirs.
const LANGUAGE_NAMES: Record<Locale, string> = {
  en: "English",
  az: "Azərbaycanca",
  tr: "Türkçe",
  ru: "Русский",
};

export function LanguageSwitcher() {
  const t = useTranslations("Common");
  const current = useLocale();
  const pathname = usePathname();

  return (
    <nav aria-label={t("language")}>
      <ul className="flex flex-wrap gap-1">
        {locales.map((locale) => (
          <li key={locale}>
            <Link
              href={pathname}
              locale={locale}
              lang={locale}
              hrefLang={locale}
              aria-current={locale === current ? "true" : undefined}
              className={cn(
                buttonVariants({ variant: locale === current ? "default" : "ghost", size: "sm" }),
              )}
            >
              {LANGUAGE_NAMES[locale]}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
