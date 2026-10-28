"use client";

import { useLocale, useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "@/components/auth/session-provider";
import { Button } from "@/components/ui/button";
import { isLocale, locales, type Locale } from "@/i18n/config";
import {
  ApiProblem,
  completeOnboarding,
  getDepartments,
  getProfessions,
  getProfile,
  getProfileOptions,
  saveProfile,
  type Department,
  type Profession,
  type Profile,
  type ProfileChange,
  type ProfileOptions,
} from "@/lib/api";
import { hardNavigate } from "@/lib/navigate";

// Each language is named in itself, so someone who cannot read this page can still find theirs.
const LANGUAGE_NAMES: Record<Locale, string> = {
  en: "English",
  az: "Azərbaycanca",
  tr: "Türkçe",
  ru: "Русский",
};

const STEPS = ["profession", "interests", "language", "aiLevel"] as const;
type Step = (typeof STEPS)[number];

type Draft = {
  professionId: string | null;
  departmentId: string | null;
  interests: string[];
  locale: string | null;
  aiLevel: string | null;
};

type Loaded = {
  profile: Profile;
  options: ProfileOptions;
  professions: Profession[];
  departments: Department[];
};

type Problem = "choose" | "saveFailed" | "unavailable" | "incomplete";

const PROBLEM_KEYS = {
  profession: "problems.choose_profession",
  interests: "problems.choose_interests",
  language: "problems.choose_language",
  aiLevel: "problems.choose_aiLevel",
  saveFailed: "problems.saveFailed",
  unavailable: "problems.unavailable",
  incomplete: "problems.incomplete",
} as const;

/** The first step without an answer, so someone who left halfway continues where they stopped. */
function firstOpenStep(profile: Profile): number {
  if (!profile.profession_id) return 0;
  if (profile.interests.length === 0) return 1;
  if (!profile.locale) return 2;
  return 3;
}

function draftOf(profile: Profile): Draft {
  return {
    professionId: profile.profession_id,
    departmentId: profile.department_id,
    interests: profile.interests,
    locale: profile.locale,
    aiLevel: profile.ai_level,
  };
}

function problemOf(error: unknown): Problem {
  if (error instanceof ApiProblem && (error.status === 0 || error.status >= 500)) {
    return "unavailable";
  }
  return "saveFailed";
}

export function OnboardingStepper() {
  const t = useTranslations("Onboarding");
  const locale = useLocale();
  const params = useSearchParams();
  const { state } = useSession();
  const [loaded, setLoaded] = useState<Loaded | "loading" | "failed">("loading");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);

  useEffect(() => {
    let current = true;
    Promise.all([getProfile(), getProfileOptions(), getProfessions(), getDepartments()]).then(
      ([profile, options, professions, departments]) => {
        if (!current) return;
        if (profile.onboarding_completed_at) {
          hardNavigate(`/${locale}`); // nothing left to ask
          return;
        }
        const asked = Number.parseInt(params.get("step") ?? "", 10);
        setDraft(draftOf(profile));
        // Never further than the first open step: the questions are answered in order.
        const open = firstOpenStep(profile);
        setStep(asked >= 1 && asked - 1 <= open ? asked - 1 : open); // NaN fails both tests
        setLoaded({ profile, options, professions, departments });
      },
      () => current && setLoaded("failed"),
    );
    return () => {
      current = false;
    };
  }, [locale, params]);

  // After a step the heading takes focus, so a screen reader announces where the person is.
  useEffect(() => {
    if (moved.current) heading.current?.focus();
    moved.current = true;
  }, [step]);

  if (loaded === "failed") {
    return (
      <p role="alert" className="rounded-md border border-border bg-muted p-4">
        {t("loadFailed")}
      </p>
    );
  }
  if (loaded === "loading" || !draft) {
    return (
      <p role="status" className="text-muted-foreground">
        {t("loading")}
      </p>
    );
  }
  if (state.status !== "authenticated") return null;
  const csrf = state.session.csrf_token;
  // The department is sent only when the person changed it: a department that came from the
  // identity provider stays the provider's until they pick one themselves.
  const initialDepartment = loaded.profile.department_id;
  const current: Step = STEPS[step]!;
  const last = step === STEPS.length - 1;
  // Choices come from the server as codes; a code this version has no text for shows as itself.
  const named = (key: string, fallback: string) =>
    t.has(key as never) ? t(key as never) : fallback;

  function change(next: Partial<Draft>) {
    setDraft((previous) => (previous ? { ...previous, ...next } : previous));
    setProblem(null);
  }

  function missing(): boolean {
    if (!draft) return true;
    switch (current) {
      case "profession":
        return !draft.professionId;
      case "interests":
        return draft.interests.length === 0;
      case "language":
        return !draft.locale;
      case "aiLevel":
        return !draft.aiLevel;
    }
  }

  function body(): ProfileChange {
    if (!draft) return {};
    switch (current) {
      case "profession":
        return draft.departmentId === initialDepartment
          ? { profession_id: draft.professionId }
          : { profession_id: draft.professionId, department_id: draft.departmentId };
      case "interests":
        return { interests: draft.interests };
      case "language":
        return { locale: draft.locale };
      case "aiLevel":
        return { ai_level: draft.aiLevel };
    }
  }

  async function next(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    if (missing()) {
      setProblem("choose");
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      await saveProfile(body(), csrf);
      if (current === "language" && isLocale(draft?.locale) && draft.locale !== locale) {
        // The page switches to the chosen language and continues with the next step.
        hardNavigate(`/${draft.locale}/onboarding?step=${step + 2}`);
        return;
      }
      if (last) {
        await completeOnboarding(csrf);
        hardNavigate(`/${locale}`);
        return;
      }
      setStep(step + 1);
    } catch (error) {
      if (error instanceof ApiProblem && error.status === 409) {
        // The server says an earlier answer is missing (or was cleared): go back to it.
        try {
          const fresh = await getProfile();
          setDraft(draftOf(fresh));
          setLoaded((previous) =>
            typeof previous === "object" ? { ...previous, profile: fresh } : previous,
          );
          setStep(firstOpenStep(fresh));
          setProblem("incomplete");
        } catch {
          setProblem("unavailable");
        }
      } else {
        setProblem(problemOf(error));
      }
    }
    setBusy(false);
  }

  return (
    <div className="space-y-6">
      <nav aria-label={t("progress")}>
        <ol className="flex flex-wrap gap-2 text-sm">
          {STEPS.map((name, index) => (
            <li
              key={name}
              aria-current={index === step ? "step" : undefined}
              className={
                index === step
                  ? "rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground"
                  : "rounded-md border border-border px-3 py-1 text-muted-foreground"
              }
            >
              {t(`steps.${name}`)}
            </li>
          ))}
        </ol>
      </nav>

      <form onSubmit={next} className="space-y-6" noValidate>
        <fieldset className="space-y-4">
          <legend className="sr-only">
            {t("stepOf", { current: step + 1, total: STEPS.length })}
          </legend>
          <h2 ref={heading} tabIndex={-1} className="text-xl font-semibold" id="step-heading">
            {t(`${current}.heading`)}
          </h2>
          <p className="text-sm text-muted-foreground">
            {t("stepOf", { current: step + 1, total: STEPS.length })}
            {" · "}
            {t(`${current}.hint`)}
          </p>

          {current === "profession" && (
            <>
              {loaded.professions.length === 0 ? (
                <p role="alert">{t("profession.none")}</p>
              ) : (
                <div role="radiogroup" aria-labelledby="step-heading" className="space-y-2">
                  {loaded.professions.map((item) => (
                    <label
                      key={item.id}
                      className="flex items-center gap-3 rounded-md border border-border p-3"
                    >
                      <input
                        type="radio"
                        name="profession"
                        value={item.id}
                        checked={draft.professionId === item.id}
                        onChange={() => change({ professionId: item.id })}
                      />
                      <span>{item.names[locale] ?? item.names.en ?? item.code}</span>
                    </label>
                  ))}
                </div>
              )}
              {loaded.departments.length > 0 && (
                <div className="space-y-1">
                  <label htmlFor="department" className="text-sm font-medium">
                    {t("profession.department")}
                  </label>
                  <select
                    id="department"
                    value={draft.departmentId ?? ""}
                    onChange={(event) => change({ departmentId: event.target.value || null })}
                    className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm"
                  >
                    <option value="">{t("profession.noDepartment")}</option>
                    {loaded.departments.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </>
          )}

          {current === "interests" && (
            <div role="group" aria-labelledby="step-heading" className="grid gap-2 sm:grid-cols-2">
              {loaded.options.interests.map((code) => (
                <label
                  key={code}
                  className="flex items-center gap-3 rounded-md border border-border p-3"
                >
                  <input
                    type="checkbox"
                    name="interests"
                    value={code}
                    checked={draft.interests.includes(code)}
                    onChange={(event) =>
                      change({
                        interests: event.target.checked
                          ? [...draft.interests, code]
                          : draft.interests.filter((item) => item !== code),
                      })
                    }
                  />
                  <span>{named(`interestNames.${code}`, code)}</span>
                </label>
              ))}
            </div>
          )}

          {current === "language" && (
            <div role="radiogroup" aria-labelledby="step-heading" className="space-y-2">
              {loaded.options.locales
                .filter((code): code is Locale => (locales as readonly string[]).includes(code))
                .map((code) => (
                  <label
                    key={code}
                    className="flex items-center gap-3 rounded-md border border-border p-3"
                  >
                    <input
                      type="radio"
                      name="language"
                      value={code}
                      checked={draft.locale === code}
                      onChange={() => change({ locale: code })}
                    />
                    <span lang={code}>{LANGUAGE_NAMES[code]}</span>
                  </label>
                ))}
            </div>
          )}

          {current === "aiLevel" && (
            <div role="radiogroup" aria-labelledby="step-heading" className="space-y-2">
              {loaded.options.ai_levels.map((code) => (
                <label
                  key={code}
                  className="flex items-start gap-3 rounded-md border border-border p-3"
                >
                  <input
                    type="radio"
                    name="ai-level"
                    value={code}
                    className="mt-1"
                    checked={draft.aiLevel === code}
                    onChange={() => change({ aiLevel: code })}
                  />
                  <span>
                    <span className="block font-medium">{named(`levels.${code}.name`, code)}</span>
                    {t.has(`levels.${code}.description` as never) && (
                      <span className="block text-sm text-muted-foreground">
                        {t(`levels.${code}.description` as never)}
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
          )}
        </fieldset>

        {problem && (
          <p role="alert" className="rounded-md border-2 border-border bg-muted p-4">
            {t(PROBLEM_KEYS[problem === "choose" ? current : problem])}
          </p>
        )}

        <div className="flex flex-wrap gap-3">
          {step > 0 && (
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setProblem(null);
                setStep(step - 1);
              }}
            >
              {t("back")}
            </Button>
          )}
          <Button type="submit" disabled={busy}>
            {busy ? t("saving") : last ? t("finish") : t("next")}
          </Button>
        </div>
      </form>
    </div>
  );
}
