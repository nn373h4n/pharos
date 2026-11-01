import type { Metadata } from "next";
import { hasLocale, NextIntlClientProvider } from "next-intl";
import { getTranslations, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";
import { connection } from "next/server";
import type { ReactNode } from "react";
import { SessionProvider } from "@/components/auth/session-provider";
import { UserMenu } from "@/components/auth/user-menu";
import { MainNav } from "@/components/layout/main-nav";
import { LanguageSwitcher } from "@/components/language-switcher";
import { routing } from "@/i18n/routing";
import "../globals.css";

type Props = { children: ReactNode; params: Promise<{ locale: string }> };

export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }));
}

export async function generateMetadata({ params }: Pick<Props, "params">): Promise<Metadata> {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) return {};
  const t = await getTranslations({ locale, namespace: "Metadata" });
  return { title: t("title"), description: t("description") };
}

export default async function LocaleLayout({ children, params }: Props) {
  // A nonce for the Content-Security-Policy is made per request (proxy.ts), so a page cannot be
  // built ahead of time: its scripts must carry the nonce of the request that gets it.
  await connection();
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) notFound();
  setRequestLocale(locale);
  const t = await getTranslations({ locale, namespace: "Common" });
  const footer = await getTranslations({ locale, namespace: "Footer" });

  return (
    <html lang={locale}>
      <body className="flex min-h-screen flex-col">
        <NextIntlClientProvider>
          <SessionProvider>
            <a
              href="#main"
              className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded-md focus:bg-primary focus:px-4 focus:py-2 focus:text-primary-foreground"
            >
              {t("skipToContent")}
            </a>
            <header className="border-b border-border">
              <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 px-4 py-3">
                <p className="text-lg font-semibold">{"Pharos"}</p>
                <MainNav />
                <div className="flex flex-wrap items-center gap-4">
                  <UserMenu />
                  <LanguageSwitcher />
                </div>
              </div>
            </header>
            {children}
            <footer className="mt-auto border-t border-border">
              <p className="mx-auto max-w-4xl px-4 py-4 text-sm text-muted-foreground">
                {footer("license")}
              </p>
            </footer>
          </SessionProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
