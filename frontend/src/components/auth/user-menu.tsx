"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { Button } from "@/components/ui/button";

const ROLE_KEYS = { admin: "roleAdmin", editor: "roleEditor", member: "roleMember" } as const;

/** Who is signed in and the way out. Shown in the header only while there is a session. */
export function UserMenu() {
  const t = useTranslations("Shell");
  const { state, signOut } = useSession();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  if (state.status !== "authenticated") return null;
  const { user } = state.session;

  async function leave() {
    setBusy(true);
    setFailed(false);
    const done = await signOut();
    if (!done) {
      setFailed(true);
      setBusy(false);
    }
  }

  return (
    <div role="group" aria-label={t("account")} className="flex flex-wrap items-center gap-3">
      <p className="text-sm">
        <span className="font-medium">{user.display_name}</span>
        <span className="text-muted-foreground">
          {" · "}
          {t(ROLE_KEYS[user.role])}
        </span>
      </p>
      <Button variant="outline" size="sm" onClick={leave} disabled={busy}>
        {busy ? t("signingOut") : t("signOut")}
      </Button>
      {failed && (
        <p role="alert" className="basis-full text-sm">
          {t("signOutFailed")}
        </p>
      )}
    </div>
  );
}
