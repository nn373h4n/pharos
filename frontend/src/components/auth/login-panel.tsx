"use client";

import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { useEffect, useId, useState, type FormEvent } from "react";
import { useSession } from "@/components/auth/session-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { hardNavigate } from "@/lib/navigate";
import { ApiProblem, getAuthConfig, localLogin, safeNext, type AuthConfig } from "@/lib/api";

type FailureKey =
  | "invalid_credentials"
  | "rate_limited"
  | "sso_failed"
  | "sso_denied"
  | "sso_unavailable"
  | "account_disabled"
  | "unavailable"
  | "generic";

// Codes the backend puts in `/login?error=` after a single sign-on attempt (docs/API.md).
const REDIRECT_ERRORS: readonly FailureKey[] = [
  "sso_failed",
  "sso_denied",
  "sso_unavailable",
  "account_disabled",
];

type Failure = { key: FailureKey; seconds?: number };

function failureOf(error: unknown): Failure {
  if (error instanceof ApiProblem) {
    if (error.code === "invalid_credentials") return { key: "invalid_credentials" };
    if (error.code === "rate_limited")
      return { key: "rate_limited", seconds: error.retryAfter ?? 60 };
    if (error.status === 0 || error.status >= 500) return { key: "unavailable" };
  }
  return { key: "generic" };
}

export function LoginPanel() {
  const t = useTranslations("Login");
  const errors = useTranslations("LoginErrors");
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const redirectError = params.get("error");
  const { state, setSession } = useSession();

  const [config, setConfig] = useState<AuthConfig | "loading" | "failed">("loading");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const alertId = useId();

  useEffect(() => {
    let current = true;
    getAuthConfig().then(
      (loaded) => current && setConfig(loaded),
      () => current && setConfig("failed"),
    );
    return () => {
      current = false;
    };
  }, []);

  // Someone who is already signed in has nothing to do here.
  useEffect(() => {
    if (state.status === "authenticated") hardNavigate(next);
  }, [state.status, next]);

  const fromRedirect = REDIRECT_ERRORS.find((known) => known === redirectError);
  const shown: Failure | null =
    failure ?? (fromRedirect ? { key: fromRedirect } : redirectError ? { key: "generic" } : null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const session = await localLogin(username, password);
      setSession(session);
      hardNavigate(next);
    } catch (error) {
      setFailure(failureOf(error));
      setPassword("");
      setBusy(false);
    }
  }

  if (config === "loading") {
    return (
      <p role="status" className="text-muted-foreground">
        {t("loading")}
      </p>
    );
  }
  if (config === "failed") {
    return (
      <p role="alert" className="rounded-md border border-border bg-muted p-4">
        {t("loadFailed")}
      </p>
    );
  }

  const passwordSignIn = config.local_login !== "off";
  const ssoUrl = config.oidc ? `${config.oidc.login_url}?next=${encodeURIComponent(next)}` : null;

  return (
    <div className="space-y-6">
      {shown && (
        <p id={alertId} role="alert" className="rounded-md border-2 border-border bg-muted p-4">
          {errors(shown.key, { seconds: shown.seconds ?? 0 })}
        </p>
      )}

      {config.oidc && ssoUrl && (
        <Button asChild size="lg" className="w-full">
          <a href={ssoUrl}>{t("ssoButton", { provider: config.oidc.provider_name })}</a>
        </Button>
      )}

      {config.oidc && passwordSignIn && (
        <p aria-hidden="true" className="text-center text-sm text-muted-foreground">
          {t("or")}
        </p>
      )}

      {passwordSignIn && (
        <form onSubmit={submit} className="space-y-4" aria-labelledby="password-heading">
          <h2 id="password-heading" className="text-lg font-semibold">
            {t("passwordHeading")}
          </h2>
          {config.local_login === "break-glass" && (
            <p className="text-sm text-muted-foreground">{t("breakGlassHint")}</p>
          )}
          <div className="space-y-1">
            <label htmlFor="username" className="text-sm font-medium">
              {t("username")}
            </label>
            <Input
              id="username"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              maxLength={256}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              aria-invalid={shown?.key === "invalid_credentials" ? true : undefined}
              aria-describedby={shown ? alertId : undefined}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="password" className="text-sm font-medium">
              {t("password")}
            </label>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              maxLength={1024}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={shown?.key === "invalid_credentials" ? true : undefined}
              aria-describedby={shown ? alertId : undefined}
            />
          </div>
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? t("submitting") : t("submit")}
          </Button>
        </form>
      )}

      {!config.oidc && !passwordSignIn && <p>{t("noMethods")}</p>}
    </div>
  );
}
