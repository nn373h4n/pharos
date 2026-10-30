"use client";

import { useLocale, useTranslations } from "next-intl";
import { FeedArticles } from "@/components/feeds/feed-articles";
import { RunsPanel } from "@/components/sources/runs-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FEED_INTERVALS, healthOf, listFeedRuns, type Feed } from "@/lib/feeds";
import { formatDateTime } from "@/lib/format";
import { errorKey } from "@/lib/source-text";

type Props = {
  feed: Feed;
  busy: boolean;
  onToggle: (feed: Feed) => void;
  onEdit: (feed: Feed) => void;
  onDelete: (feed: Feed) => void;
};

/**
 * One feed of the signed-in person. The name and the address are shown as text: the address is
 * not made a link, as it is typed by the person and may be wrong.
 */
export function FeedCard({ feed, busy, onToggle, onEdit, onDelete }: Props) {
  const s = useTranslations("Sources");
  const locale = useLocale();
  const health = healthOf(feed);
  const lastSuccess = formatDateTime(feed.last_success_at, locale);
  const next = formatDateTime(feed.next_fetch_at, locale);
  const every = (FEED_INTERVALS as readonly number[]).includes(feed.interval_seconds)
    ? s(`intervals.${feed.interval_seconds as (typeof FEED_INTERVALS)[number]}`)
    : s("intervalOther", { seconds: feed.interval_seconds });
  const name = feed.name;
  const label = (action: "edit" | "enable" | "disable" | "delete") =>
    s("actions.actionOn", { action: s(`actions.${action}`), name });

  return (
    <li className="rounded-lg border border-border p-4 break-words">
      <article aria-labelledby={`feed-${feed.id}`}>
        <h3 id={`feed-${feed.id}`} className="text-lg font-semibold">
          {name}
        </h3>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Badge tone={health === "ok" ? "plain" : "strong"}>{s(`health.${health}`)}</Badge>
        </div>
        <p className="mt-2 break-all text-sm">{feed.url}</p>
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
          <li>{every}</li>
          <li>{lastSuccess ? s("facts.lastSuccess", { date: lastSuccess }) : s("facts.never")}</li>
          {feed.consecutive_failures > 0 && (
            <li>{s("facts.failures", { count: feed.consecutive_failures })}</li>
          )}
          {feed.last_error_code && (
            <li>
              {s("facts.lastError", { problem: s(`lastError.${errorKey(feed.last_error_code)}`) })}
            </li>
          )}
          {feed.enabled && next && <li>{s("facts.next", { date: next })}</li>}
        </ul>

        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            aria-label={label("edit")}
            onClick={() => onEdit(feed)}
          >
            {s("actions.edit")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            aria-label={label(feed.enabled ? "disable" : "enable")}
            onClick={() => onToggle(feed)}
          >
            {s(feed.enabled ? "actions.disable" : "actions.enable")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            aria-label={label("delete")}
            onClick={() => onDelete(feed)}
          >
            {s("actions.delete")}
          </Button>
        </div>
        <FeedArticles feed={feed} />
        <RunsPanel source={feed} load={listFeedRuns} />
      </article>
    </li>
  );
}
