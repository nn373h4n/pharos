import { hasLocale } from "next-intl";
import { getTranslations, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";
import { Welcome } from "@/components/auth/welcome";
import { routing } from "@/i18n/routing";

type Props = { params: Promise<{ locale: string }> };

export default async function HomePage({ params }: Props) {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) notFound();
  setRequestLocale(locale);
  const t = await getTranslations("Home");

  return (
    <main id="main" tabIndex={-1} className="mx-auto w-full max-w-4xl flex-1 px-4 py-12">
      <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">{t("title")}</h1>
      <Welcome />
      <p className="mt-4 max-w-2xl text-lg text-muted-foreground">{t("subtitle")}</p>
      <section
        aria-labelledby="status"
        className="mt-10 rounded-lg border border-border bg-muted p-6"
      >
        <h2 id="status" className="text-lg font-semibold">
          {t("statusTitle")}
        </h2>
        <p className="mt-2 text-muted-foreground">{t("statusBody")}</p>
      </section>
    </main>
  );
}
