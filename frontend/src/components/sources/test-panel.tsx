"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiProblem } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { hostOf, safeHref } from "@/lib/links";
import { problemKey, type ProblemKey } from "@/lib/problems";
import { errorKey, outcomeKey } from "@/lib/source-text";
import {
  readTestFetch,
  startTestFetch,
  TEST_GIVE_UP_MS,
  TEST_POLL_MS,
  type Source,
  type TestAnswer,
} from "@/lib/sources";

type State =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "waiting"; testId: string }
  | { phase: "answer"; answer: TestAnswer }
  | { phase: "gaveUp" }
  | { phase: "failed"; problem: ProblemKey; seconds: number | null };

type Props = { source: Source; csrf: string };

/**
 * One trial fetch of a source, done by the worker. The page asks for it, then asks every two
 * seconds for the answer, for two minutes at the most. What comes back is text from a third party
 * and from a process that handles hostile content: it is shown as text only.
 */
export function TestPanel({ source, csrf }: Props) {
  const t = useTranslations("Sources");
  const problems = useTranslations("Problems");
  const locale = useLocale();
  const [state, setState] = useState<State>({ phase: "idle" });
  const waitingFor = state.phase === "waiting" ? state.testId : null;

  useEffect(() => {
    if (waitingFor === null) return;
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();
    const poll = async () => {
      try {
        const answer = await readTestFetch(source.id, waitingFor);
        if (!current) return;
        if (answer.status !== "pending") {
          setState({ phase: "answer", answer });
          return;
        }
      } catch (error) {
        if (!current) return;
        setState({
          phase: "failed",
          problem: problemKey(error),
          seconds: error instanceof ApiProblem ? error.retryAfter : null,
        });
        return;
      }
      if (Date.now() - startedAt >= TEST_GIVE_UP_MS) {
        setState({ phase: "gaveUp" });
        return;
      }
      timer = setTimeout(poll, TEST_POLL_MS);
    };
    timer = setTimeout(poll, TEST_POLL_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [source.id, waitingFor]);

  async function start() {
    setState({ phase: "starting" });
    try {
      const started = await startTestFetch(source.id, csrf);
      setState({ phase: "waiting", testId: started.test_id });
    } catch (error) {
      setState({
        phase: "failed",
        problem: problemKey(error),
        seconds: error instanceof ApiProblem ? error.retryAfter : null,
      });
    }
  }

  const running = state.phase === "starting" || state.phase === "waiting";
  const name = source.name;

  return (
    <div className="mt-3">
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={running}
        aria-label={t("actions.actionOn", { action: t("actions.test"), name })}
        onClick={start}
      >
        {t("actions.test")}
      </Button>
      <div role="status" className="mt-2 min-h-5 text-sm">
        {state.phase === "starting" || state.phase === "waiting" ? t("test.waiting") : null}
        {state.phase === "gaveUp" && t("test.gaveUp")}
        {state.phase === "failed" &&
          (state.problem === "rateLimited" && state.seconds === null
            ? problems("rateLimitedNoWait")
            : problems(state.problem, { seconds: state.seconds ?? 0 }))}
        {state.phase === "answer" && (
          <Answer
            answer={state.answer}
            locale={locale}
            onClose={() => setState({ phase: "idle" })}
          />
        )}
      </div>
    </div>
  );
}

function Answer({
  answer,
  locale,
  onClose,
}: {
  answer: TestAnswer;
  locale: string;
  onClose: () => void;
}) {
  const t = useTranslations("Sources");
  if (answer.status === "unavailable") {
    return (
      <div className="rounded-md border border-border bg-muted p-3">
        <p>{t("test.unavailable")}</p>
        <CloseButton onClose={onClose} />
      </div>
    );
  }
  return (
    <div className="rounded-md border border-border bg-muted p-3">
      <h4 className="font-semibold">{t("test.title")}</h4>
      <p className="mt-1">
        {t("test.result", { outcome: t(`outcome.${outcomeKey(answer.outcome)}`) })}
      </p>
      {answer.error_code && (
        <p>{t("test.problem", { problem: t(`lastError.${errorKey(answer.error_code)}`) })}</p>
      )}
      {answer.http_status !== null && <p>{t("test.status", { status: answer.http_status })}</p>}
      {answer.outcome === "ok" && (
        <>
          <p>
            {t("test.counts", {
              seen: answer.entries_seen ?? 0,
              skipped: answer.entries_skipped ?? 0,
            })}
          </p>
          {answer.sample.length === 0 ? (
            <p>{t("test.noSample")}</p>
          ) : (
            <>
              <h5 className="mt-2 font-medium">{t("test.sample")}</h5>
              <ul className="mt-1 list-disc space-y-1 pl-5">
                {answer.sample.map((found, index) => {
                  const href = safeHref(found.url);
                  const date = formatDate(found.published_at, locale);
                  return (
                    <li key={`${index}-${found.url}`} className="break-words">
                      {href ? (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer nofollow"
                          className="underline underline-offset-4 hover:no-underline"
                        >
                          {found.title}
                          <span className="sr-only">{t("test.opensInNewTab")}</span>
                        </a>
                      ) : (
                        found.title
                      )}
                      {href && (
                        <span className="text-muted-foreground">{` · ${hostOf(href)}`}</span>
                      )}
                      {date && <span className="text-muted-foreground">{` · ${date}`}</span>}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </>
      )}
      <CloseButton onClose={onClose} />
    </div>
  );
}

function CloseButton({ onClose }: { onClose: () => void }) {
  const t = useTranslations("Sources");
  return (
    <Button type="button" variant="outline" size="sm" className="mt-3" onClick={onClose}>
      {t("test.close")}
    </Button>
  );
}
