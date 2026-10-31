"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { Button } from "@/components/ui/button";
import { ApiProblem } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import {
  dollars,
  getStatus,
  KEY_GIVE_UP_MS,
  KEY_POLL_MS,
  readKeyTest,
  spentPercent,
  startKeyTest,
  type KeyTestAnswer,
  type LlmStatus,
} from "@/lib/llm";
import { problemKey, type ProblemKey } from "@/lib/problems";

type Loaded = { status: LlmStatus } | { failed: ProblemKey };
type Check =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "waiting"; testId: string }
  | { phase: "answer"; answer: KeyTestAnswer }
  | { phase: "gaveUp" }
  | { phase: "failed"; problem: ProblemKey; seconds: number | null };

/**
 * The model layer as an administrator sees it: what is set, what has been spent today, how the last
 * calls went, and a button that asks the worker to try the key. Nothing here is secret and the key
 * is never shown; what the worker answers is read as one word from a fixed list.
 */
export function LlmView() {
  const t = useTranslations("Llm");
  const problems = useTranslations("Problems");
  const locale = useLocale();
  const { state } = useSession();
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [reloads, setReloads] = useState(0);
  const [check, setCheck] = useState<Check>({ phase: "idle" });
  const role = state.status === "authenticated" ? state.session.user.role : null;
  const csrf = state.status === "authenticated" ? state.session.csrf_token : "";
  const waitingFor = check.phase === "waiting" ? check.testId : null;

  useEffect(() => {
    if (role === null) return;
    let current = true;
    getStatus().then(
      (status) => {
        if (current) setLoaded({ status });
      },
      (error) => {
        if (current) setLoaded({ failed: problemKey(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [role, reloads]);

  useEffect(() => {
    if (waitingFor === null) return;
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();
    const poll = async () => {
      try {
        const answer = await readKeyTest(waitingFor);
        if (!current) return;
        if (answer.status !== "pending") {
          setCheck({ phase: "answer", answer });
          setReloads((n) => n + 1);
          return;
        }
      } catch (error) {
        if (!current) return;
        setCheck({
          phase: "failed",
          problem: problemKey(error),
          seconds: error instanceof ApiProblem ? error.retryAfter : null,
        });
        return;
      }
      if (Date.now() - startedAt >= KEY_GIVE_UP_MS) {
        setCheck({ phase: "gaveUp" });
        return;
      }
      timer = setTimeout(poll, KEY_POLL_MS);
    };
    timer = setTimeout(poll, KEY_POLL_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [waitingFor]);

  async function start() {
    setCheck({ phase: "starting" });
    try {
      const started = await startKeyTest(csrf);
      setCheck({ phase: "waiting", testId: started.test_id });
    } catch (error) {
      setCheck({
        phase: "failed",
        problem: problemKey(error),
        seconds: error instanceof ApiProblem ? error.retryAfter : null,
      });
    }
  }

  if (role === null) return null;
  if (loaded === null) {
    return (
      <p role="status" className="mt-6 text-muted-foreground">
        {t("loading")}
      </p>
    );
  }
  if ("failed" in loaded) {
    return (
      <div className="mt-6">
        <p role="alert">
          {loaded.failed === "forbidden" ? problems("forbidden") : t("loadFailed")}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          onClick={() => {
            setLoaded(null);
            setReloads((n) => n + 1);
          }}
        >
          {t("retry")}
        </Button>
      </div>
    );
  }

  const { status } = loaded;
  const percent = spentPercent(status.spent_micro_usd, status.daily_cap_micro_usd);
  const running = check.phase === "starting" || check.phase === "waiting";
  const lastCheck = status.key_check;
  const when = lastCheck ? formatDateTime(lastCheck.checked_at, locale) : null;

  return (
    <div className="mt-6 space-y-8">
      <p className="text-muted-foreground">{t("intro")}</p>
      {status.provider === "off" && <p role="note">{t("offNote")}</p>}

      <section aria-labelledby="llm-setup">
        <h2 id="llm-setup" className="text-xl font-semibold">
          {t("setup.title")}
        </h2>
        <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
          <dt className="font-medium">{t("setup.provider")}</dt>
          <dd>{t(`provider.${status.provider}`)}</dd>
          {(["filter", "enrichment", "insight"] as const).map((stage) => (
            <div key={stage} className="contents">
              <dt className="font-medium">{t(`stage.${stage}`)}</dt>
              <dd className="break-all">{status.models[stage] || t("setup.noModel")}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-2 text-sm text-muted-foreground">{t("setup.keyNote")}</p>
      </section>

      <section aria-labelledby="llm-budget">
        <h2 id="llm-budget" className="text-xl font-semibold">
          {t("budget.title")}
        </h2>
        <p className="mt-3 text-sm">
          {t("budget.spent", {
            spent: dollars(status.spent_micro_usd, locale),
            cap: dollars(status.daily_cap_micro_usd, locale),
            percent,
          })}
        </p>
        <div
          role="progressbar"
          aria-label={t("budget.title")}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          className="mt-2 h-3 w-full overflow-hidden rounded-full border border-border bg-muted"
        >
          <div className="h-full bg-primary" style={{ width: `${percent}%` }} />
        </div>
        {status.exhausted ? (
          <p role="alert" className="mt-3 text-sm font-medium">
            {t("budget.exhausted")}
          </p>
        ) : (
          status.warned && (
            <p role="alert" className="mt-3 text-sm font-medium">
              {t("budget.warned")}
            </p>
          )
        )}
      </section>

      <section aria-labelledby="llm-key">
        <h2 id="llm-key" className="text-xl font-semibold">
          {t("key.title")}
        </h2>
        <p className="mt-3 text-sm">
          {lastCheck
            ? t("key.last", { outcome: t(`outcome.${lastCheck.outcome}`), when: when ?? "" })
            : t("key.never")}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-3"
          disabled={running}
          onClick={start}
        >
          {t("key.test")}
        </Button>
        <div role="status" className="mt-2 min-h-5 text-sm">
          {running && t("key.waiting")}
          {check.phase === "gaveUp" && t("key.gaveUp")}
          {check.phase === "failed" &&
            (check.problem === "rateLimited" && check.seconds === null
              ? problems("rateLimitedNoWait")
              : problems(check.problem, { seconds: check.seconds ?? 0 }))}
          {check.phase === "answer" &&
            (check.answer.status === "done" && check.answer.outcome
              ? t("key.result", { outcome: t(`outcome.${check.answer.outcome}`) })
              : t("key.unavailable"))}
        </div>
      </section>

      <section aria-labelledby="llm-usage">
        <h2 id="llm-usage" className="text-xl font-semibold">
          {t("usage.title")}
        </h2>
        {status.usage.length === 0 ? (
          <p className="mt-3 text-sm">{t("usage.empty")}</p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">{t("usage.title")}</caption>
              <thead>
                <tr className="border-b border-border">
                  {(["day", "stage", "calls", "refused", "failed", "tokens", "cost"] as const).map(
                    (column) => (
                      <th key={column} scope="col" className="py-2 pr-4 font-medium">
                        {t(`usage.${column}`)}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {status.usage.map((row) => (
                  <tr key={`${row.day}-${row.stage}`} className="border-b border-border">
                    <td className="py-2 pr-4">{row.day}</td>
                    <td className="py-2 pr-4">{t(`stage.${row.stage}`)}</td>
                    <td className="py-2 pr-4">{row.calls}</td>
                    <td className="py-2 pr-4">{row.refused}</td>
                    <td className="py-2 pr-4">{row.failed}</td>
                    <td className="py-2 pr-4">{`${row.tokens_in} / ${row.tokens_out}`}</td>
                    <td className="py-2 pr-4">{dollars(row.cost_micro_usd, locale)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
