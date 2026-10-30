"use client";

import { useLocale, useTranslations } from "next-intl";
import { RunsPanel } from "@/components/sources/runs-panel";
import { TestPanel } from "@/components/sources/test-panel";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { formatDateTime } from "@/lib/format";
import { errorKey } from "@/lib/source-text";
import { healthOf, INTERVALS, summaryOf, type Source } from "@/lib/sources";
import { cn } from "@/lib/utils";

type Props = {
  source: Source;
  /** Administrators change sources; editors only look at them. */
  admin: boolean;
  csrf: string;
  /** True while any step is being sent: the buttons wait. */
  busy: boolean;
  onToggle: (source: Source) => void;
  onEdit: (source: Source) => void;
  onDelete: (source: Source) => void;
};

/**
 * One source: what it fetches, how it is doing, and (for administrators) what can be done with
 * it. The name and the address are shown as text; the address of a feed is not made a link.
 */
export function SourceCard({ source, admin, csrf, busy, onToggle, onEdit, onDelete }: Props) {
  const t = useTranslations("Sources");
  const locale = useLocale();
  const health = healthOf(source);
  const lastSuccess = formatDateTime(source.last_success_at, locale);
  const next = formatDateTime(source.next_fetch_at, locale);
  const every = (INTERVALS as readonly number[]).includes(source.interval_seconds)
    ? t(`intervals.${source.interval_seconds as (typeof INTERVALS)[number]}`)
    : t("intervalOther", { seconds: source.interval_seconds });
  const name = source.name;
  const label = (action: "edit" | "enable" | "disable" | "delete") =>
    t("actions.actionOn", { action: t(`actions.${action}`), name });

  return (
    <li className="rounded-lg border border-border p-4 break-words">
      <article aria-labelledby={`source-${source.id}`}>
        <h3 id={`source-${source.id}`} className="text-lg font-semibold">
          {name}
        </h3>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge>{t(`types.${source.type}`)}</Badge>
          <Badge tone={health === "ok" ? "plain" : "strong"}>{t(`health.${health}`)}</Badge>
        </div>
        <p className="mt-2 break-all text-sm">{summaryOf(source)}</p>
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
          <li>{every}</li>
          <li>{lastSuccess ? t("facts.lastSuccess", { date: lastSuccess }) : t("facts.never")}</li>
          {source.consecutive_failures > 0 && (
            <li>{t("facts.failures", { count: source.consecutive_failures })}</li>
          )}
          {source.last_error_code && (
            <li>
              {t("facts.lastError", {
                problem: t(`lastError.${errorKey(source.last_error_code)}`),
              })}
            </li>
          )}
          {source.enabled && next && <li>{t("facts.next", { date: next })}</li>}
        </ul>

        <div className="mt-3 flex flex-wrap gap-2">
          <Link
            href={`/items?source=${source.id}`}
            className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
            aria-label={t("actions.actionOn", { action: t("actions.articles"), name })}
          >
            {t("actions.articles")}
          </Link>
          {admin && (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                aria-label={label("edit")}
                onClick={() => onEdit(source)}
              >
                {t("actions.edit")}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                aria-label={label(source.enabled ? "disable" : "enable")}
                onClick={() => onToggle(source)}
              >
                {t(source.enabled ? "actions.disable" : "actions.enable")}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                aria-label={label("delete")}
                onClick={() => onDelete(source)}
              >
                {t("actions.delete")}
              </Button>
            </>
          )}
        </div>
        {admin && <TestPanel source={source} csrf={csrf} />}
        <RunsPanel source={source} />
      </article>
    </li>
  );
}
