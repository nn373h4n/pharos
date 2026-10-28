"use client";

import { useTranslations } from "next-intl";
import { useEffect, type ReactNode } from "react";
import { useSession } from "@/components/auth/session-provider";
import { useRouter } from "@/i18n/navigation";

/**
 * Shows its children only to a signed-in person. Hiding the page is not authorization (the
 * backend answers 401 or 403 to every call); this just sends people to sign in instead of
 * showing a page that cannot load.
 */
export function AppGate({ children }: { children: ReactNode }) {
  const t = useTranslations("Shell");
  const router = useRouter();
  const { state } = useSession();

  useEffect(() => {
    if (state.status !== "anonymous") return;
    // The address as the browser knows it, locale prefix included: the login page returns here.
    const here = window.location.pathname + window.location.search;
    router.replace({ pathname: "/login", query: { next: here } });
  }, [state.status, router]);

  if (state.status === "authenticated") return <>{children}</>;
  if (state.status === "error") {
    return (
      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-4xl flex-1 px-4 py-12">
        <p role="alert" className="rounded-md border border-border bg-muted p-4">
          {t("unavailable")}
        </p>
      </main>
    );
  }
  return (
    <main id="main" tabIndex={-1} className="mx-auto w-full max-w-4xl flex-1 px-4 py-12">
      <p role="status" className="text-muted-foreground">
        {t("loading")}
      </p>
    </main>
  );
}
