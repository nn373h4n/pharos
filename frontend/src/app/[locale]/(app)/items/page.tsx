import type { Metadata } from "next";
import { hasLocale } from "next-intl";
import { getTranslations, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";
import { ItemsView } from "@/components/items/items-view";
import { routing } from "@/i18n/routing";
import { isIdentifier } from "@/lib/ids";

type Props = {
  params: Promise<{ locale: string }>;
  searchParams: Promise<{ source?: string | string[] }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) return {};
  const t = await getTranslations({ locale, namespace: "Items" });
  return { title: t("metaTitle") };
}

export default async function ItemsPage({ params, searchParams }: Props) {
  const { locale } = await params;
  const { source } = await searchParams;
  if (!hasLocale(routing.locales, locale)) notFound();
  setRequestLocale(locale);
  const t = await getTranslations("Items");

  return (
    <main id="main" tabIndex={-1} className="mx-auto w-full max-w-4xl flex-1 px-4 py-12">
      <h1 className="text-3xl font-bold tracking-tight">{t("title")}</h1>
      <ItemsView sourceId={isIdentifier(source) ? source : undefined} />
    </main>
  );
}
