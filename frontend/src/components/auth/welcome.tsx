"use client";

import { useTranslations } from "next-intl";
import { useSession } from "@/components/auth/session-provider";

export function Welcome() {
  const t = useTranslations("Home");
  const { state } = useSession();
  if (state.status !== "authenticated") return null;
  return (
    <p className="mt-6 text-lg font-medium">
      {t("welcome", { name: state.session.user.display_name })}
    </p>
  );
}
