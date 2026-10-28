import { hasLocale } from "next-intl";
import { getTranslations, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { OnboardingStepper } from "@/components/onboarding/onboarding-stepper";
import { routing } from "@/i18n/routing";

type Props = { params: Promise<{ locale: string }> };

export default async function OnboardingPage({ params }: Props) {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) notFound();
  setRequestLocale(locale);
  const t = await getTranslations("Onboarding");

  return (
    <main id="main" tabIndex={-1} className="mx-auto w-full max-w-2xl flex-1 px-4 py-12">
      <h1 className="text-3xl font-bold tracking-tight">{t("title")}</h1>
      <p className="mt-2 text-muted-foreground">{t("intro")}</p>
      <div className="mt-8">
        <Suspense fallback={null}>
          <OnboardingStepper />
        </Suspense>
      </div>
    </main>
  );
}
