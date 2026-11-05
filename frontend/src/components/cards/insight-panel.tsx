"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiProblem } from "@/lib/api";
import { approvedTools, toolHref, type AiTool } from "@/lib/ai-tools";
import { hostOf } from "@/lib/links";
import {
  getInsight,
  INSIGHT_GIVE_UP_MS,
  INSIGHT_POLL_MS,
  parseMinutes,
  putInteraction,
  type Insight,
} from "@/lib/cards";
import { problemKey, type ProblemKey } from "@/lib/problems";

type State =
  | { phase: "loading" }
  | { phase: "pending" }
  | { phase: "ready"; insight: Insight }
  | { phase: "unavailable"; reason: "off" | "rejected" | "invalid" }
  | { phase: "gaveUp" }
  | { phase: "failed"; problem: ProblemKey; seconds: number | null };

type Props = { itemId: string; title: string; csrf: string };

/**
 * How an article applies to the person's profession, and one prompt to try. It is asked for when
 * the section is opened and not before, so a model is called only for a card somebody looks at.
 * What comes back was written by a model from a stranger's article: it is shown as text only (a
 * React text node, a read-only text field), never as markup, and never as a link.
 */
export function InsightPanel({ itemId, title, csrf }: Props) {
  const t = useTranslations("Cards");
  const problems = useTranslations("Problems");
  const [state, setState] = useState<State>({ phase: "loading" });
  const [notice, setNotice] = useState("");
  const [tried, setTried] = useState(false);
  const [minutes, setMinutes] = useState("");
  const [minutesInvalid, setMinutesInvalid] = useState(false);
  const [minutesDone, setMinutesDone] = useState(false);
  const [tools, setTools] = useState<AiTool[]>([]);
  const promptField = useRef<HTMLTextAreaElement>(null);
  const ids = { prompt: useId(), minutes: useId(), error: useId(), note: useId() };

  // The approved tools are asked for when there is a prompt to open them with. A failure (or no
  // tool) leaves "Copy" alone: the links are an addition, never a condition.
  const ready = state.phase === "ready";
  useEffect(() => {
    if (!ready) return;
    let current = true;
    approvedTools().then(
      (found) => {
        if (current) setTools(found);
      },
      () => {},
    );
    return () => {
      current = false;
    };
  }, [ready]);

  useEffect(() => {
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();
    const ask = async () => {
      try {
        const answer = await getInsight(itemId);
        if (!current) return;
        if (answer.status === "ready") return setState({ phase: "ready", insight: answer.insight });
        if (answer.status === "unavailable") {
          return setState({ phase: "unavailable", reason: answer.reason });
        }
        setState({ phase: "pending" });
      } catch (error) {
        if (!current) return;
        return setState({
          phase: "failed",
          problem: problemKey(error),
          seconds: error instanceof ApiProblem ? error.retryAfter : null,
        });
      }
      if (Date.now() - startedAt >= INSIGHT_GIVE_UP_MS) return setState({ phase: "gaveUp" });
      timer = setTimeout(ask, INSIGHT_POLL_MS);
    };
    void ask();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [itemId]);

  async function copy(prompt: string) {
    try {
      await navigator.clipboard.writeText(prompt);
      setNotice(t("insight.copied"));
    } catch {
      // No clipboard (an old browser, a refused permission): select the text for the person.
      promptField.current?.focus();
      promptField.current?.select();
      setNotice(t("insight.copyManual"));
    }
  }

  async function record(kind: "tried" | "time_saved", amount?: number) {
    try {
      await putInteraction(itemId, kind, csrf, amount);
      if (kind === "tried") setTried(true);
      else setMinutesDone(true);
      setNotice(t(kind === "tried" ? "insight.triedDone" : "insight.minutesDone"));
    } catch (error) {
      const key = problemKey(error);
      const wait = error instanceof ApiProblem ? error.retryAfter : null;
      setNotice(
        key === "rateLimited" && wait === null
          ? problems("rateLimitedNoWait")
          : problems(key, { seconds: wait ?? 0 }),
      );
    }
  }

  function saveMinutes() {
    const amount = parseMinutes(minutes);
    setMinutesInvalid(amount === null);
    if (amount !== null) void record("time_saved", amount);
  }

  return (
    <div className="mt-3 rounded-md border border-border bg-muted p-3 text-sm">
      {(state.phase === "loading" || state.phase === "pending") && (
        <p role="status">
          {state.phase === "loading" ? t("insight.loading") : t("insight.pending")}
        </p>
      )}
      {state.phase === "gaveUp" && <p role="status">{t("insight.gaveUp")}</p>}
      {state.phase === "unavailable" && (
        <p role="status">{t(`insight.unavailable.${state.reason}`)}</p>
      )}
      {state.phase === "failed" && (
        <p role="alert">
          {state.problem === "rateLimited" && state.seconds === null
            ? problems("rateLimitedNoWait")
            : problems(state.problem, { seconds: state.seconds ?? 0 })}
        </p>
      )}
      {state.phase === "ready" && (
        <div className="space-y-3">
          <section aria-label={t("insight.applies")}>
            <h4 className="font-semibold">{t("insight.applies")}</h4>
            <p className="mt-1 break-words">{state.insight.summary}</p>
            <ol className="mt-2 list-decimal space-y-1 pl-5">
              {state.insight.how_to_apply.map((step, index) => (
                <li key={`${index}-${step}`} className="break-words">
                  {step}
                </li>
              ))}
            </ol>
          </section>
          <section>
            <label htmlFor={ids.prompt} className="font-semibold">
              {t("insight.promptLabel")}
            </label>
            <textarea
              id={ids.prompt}
              ref={promptField}
              readOnly
              rows={5}
              value={state.insight.try_prompt}
              aria-describedby={ids.note}
              className="mt-1 w-full rounded-md border border-border bg-background p-2 font-mono text-sm break-words"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-2"
              aria-label={t("actionOn", { action: t("insight.copy"), title })}
              onClick={() => copy(state.insight.try_prompt)}
            >
              {t("insight.copy")}
            </Button>
            <OpenIn tools={tools} />
          </section>
          <section id={ids.note} aria-label={t("insight.noteTitle")}>
            <h4 className="font-semibold">{t("insight.noteTitle")}</h4>
            <p className="mt-1 break-words">{state.insight.safety_note}</p>
            <p className="mt-1">{t("insight.productNote")}</p>
          </section>
          <section aria-label={t("insight.feedback")} className="space-y-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={tried}
              aria-label={t("actionOn", { action: t("insight.tried"), title })}
              onClick={() => record("tried")}
            >
              {t("insight.tried")}
            </Button>
            <div className="flex flex-wrap items-end gap-2">
              <div>
                <label htmlFor={ids.minutes} className="block font-medium">
                  {t("insight.minutesLabel")}
                </label>
                <input
                  id={ids.minutes}
                  type="text"
                  inputMode="numeric"
                  autoComplete="off"
                  value={minutes}
                  maxLength={3}
                  aria-invalid={minutesInvalid}
                  aria-describedby={minutesInvalid ? ids.error : undefined}
                  onChange={(event) => {
                    setMinutes(event.target.value);
                    setMinutesInvalid(false);
                    setMinutesDone(false);
                  }}
                  className="mt-1 h-9 w-24 rounded-md border border-border bg-background px-2"
                />
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={minutesDone}
                aria-label={t("actionOn", { action: t("insight.minutesSave"), title })}
                onClick={saveMinutes}
              >
                {t("insight.minutesSave")}
              </Button>
            </div>
            {minutesInvalid && (
              <p id={ids.error} role="alert">
                {t("insight.minutesInvalid")}
              </p>
            )}
          </section>
        </div>
      )}
      <p role="status" className="mt-2 min-h-5">
        {notice}
      </p>
    </div>
  );
}

/**
 * A link to each approved AI tool, next to "Copy". The prompt is NOT put in the address: a person
 * copies it first and pastes it there, so it never reaches the tool's server logs or the browser
 * history from here. The link opens a new tab and sends no referrer. A tool whose address does not
 * pass `toolHref` is left out.
 */
function OpenIn({ tools }: { tools: AiTool[] }) {
  const t = useTranslations("Cards.insight");
  const links = tools.flatMap((tool) => {
    const href = toolHref(tool.url);
    return href ? [{ tool, href }] : [];
  });
  if (links.length === 0) return null;
  return (
    <div className="mt-3">
      <p className="text-sm font-medium">{t("openInTitle")}</p>
      <ul className="mt-1 flex flex-wrap gap-2">
        {links.map(({ tool, href }) => (
          <li key={tool.id}>
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center rounded-md border border-border px-3 py-1 text-sm underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
            >
              {t("openIn", { tool: tool.name })}
              <span className="ml-2 break-all text-xs text-muted-foreground">{hostOf(href)}</span>
              <span className="sr-only"> {t("newTab")}</span>
            </a>
          </li>
        ))}
      </ul>
      <p className="mt-1 text-sm text-muted-foreground">{t("openInNote")}</p>
    </div>
  );
}
