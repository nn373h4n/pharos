import { useTranslations } from "next-intl";
import { buttonVariants } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";

export default function NotFound() {
  const t = useTranslations("NotFound");

  return (
    <main id="main" tabIndex={-1} className="mx-auto w-full max-w-4xl flex-1 px-4 py-12">
      <h1 className="text-2xl font-bold">{t("title")}</h1>
      <p className="mt-3 text-muted-foreground">{t("body")}</p>
      <Link href="/" className={buttonVariants({ className: "mt-6" })}>
        {t("home")}
      </Link>
    </main>
  );
}
