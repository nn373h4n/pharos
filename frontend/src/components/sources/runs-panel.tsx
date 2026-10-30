"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { formatDateTime } from "@/lib/format";
import { errorKey, outcomeKey } from "@/lib/source-text";
import { listRuns, type Run, type Source } from "@/lib/sources";

type State =
  | { phase: "closed" }
  | { phase: "loading" }
  | { phase: "failed" }
  | { phase: "ready"; runs: Run[] };

/** The latest fetches of a source, loaded when the section is opened and not before. */
export function RunsPanel({ source }: { source: Source }) {
  const t = useTranslations("Sources");
  const locale = useLocale();
  const [state, setState] = useState<State>({ phase: "closed" });
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  function toggled(event: SyntheticEvent<HTMLDetailsElement>) {
    if (!event.currentTarget.open || state.phase === "loading" || state.phase === "ready") return;
    setState({ phase: "loading" });
    listRuns(source.id).then(
      (runs) => {
        if (mounted.current) setState({ phase: "ready", runs });
      },
      () => {
        if (mounted.current) setState({ phase: "failed" });
      },
    );
  }

  return (
    <details onToggle={toggled} className="mt-3">
      <summary className="cursor-pointer text-sm font-medium">{t("actions.runs")}</summary>
      <div className="mt-2" aria-live="polite">
        {state.phase === "loading" && (
          <p className="text-sm text-muted-foreground">{t("runs.loading")}</p>
        )}
        {state.phase === "failed" && <p className="text-sm">{t("runs.failed")}</p>}
        {state.phase === "ready" && state.runs.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("runs.empty")}</p>
        )}
        {state.phase === "ready" && state.runs.length > 0 && (
          <div
            role="region"
            aria-label={t("runs.title")}
            // A region that scrolls sideways must be reachable with the keyboard (WCAG 2.1.1; axe:
            // scrollable-region-focusable), which the rule against tabindex on a plain element misses.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
            tabIndex={0}
            className="overflow-x-auto rounded-md border border-border"
          >
            <table className="w-full min-w-[32rem] text-left text-sm">
              <caption className="sr-only">{t("runs.title")}</caption>
              <thead className="bg-muted">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t("runs.started")}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t("runs.result")}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t("runs.http")}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t("runs.seen")}
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    {t("runs.added")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {state.runs.map((run) => (
                  <tr key={run.id} className="border-t border-border">
                    <td className="px-3 py-2">{formatDateTime(run.started_at, locale)}</td>
                    <td className="px-3 py-2">
                      {t(`outcome.${outcomeKey(run.outcome)}`)}
                      {run.error_code && (
                        <span className="block text-muted-foreground">
                          {t(`lastError.${errorKey(run.error_code)}`)}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">{run.http_status ?? "–"}</td>
                    <td className="px-3 py-2">{run.items_seen ?? "–"}</td>
                    <td className="px-3 py-2">{run.items_new ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </details>
  );
}
