"use client";

import { useLocale, useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import { ACTIONS_FOR, type Item, type ItemAction } from "@/lib/items";
import { hostOf, safeHref } from "@/lib/links";

const REASONS = ["link_ip_host", "link_odd_port", "instruction_phrase", "editor_hold"] as const;
type Reason = (typeof REASONS)[number];

/** The words the backend uses for why an article is held; anything else is just "flagged". */
function reasonOf(code: string | null): Reason | "other" | null {
  if (code === null) return null;
  return (REASONS as readonly string[]).includes(code) ? (code as Reason) : "other";
}

type Props = {
  item: Item;
  /** Administrators and editors see the state, the reason and the sources, and can review. */
  reviewer: boolean;
  /** True while a step for this article is being sent. */
  busy: boolean;
  onAction: (item: Item, action: ItemAction) => void;
};

/**
 * One article. Its title, excerpt and sources are text from third parties and are rendered as
 * text, never as markup. The link is made only for an http(s) address, opens in a new tab with no
 * opener and no referrer, and shows its host so people see where it goes.
 */
export function ItemCard({ item, reviewer, busy, onAction }: Props) {
  const t = useTranslations("Items");
  const locale = useLocale();
  const href = safeHref(item.url);
  const published = formatDate(item.published_at, locale);
  const reasonKey = reasonOf(item.hold_reason);

  return (
    <li className="rounded-lg border border-border p-4 break-words">
      <article aria-labelledby={`item-${item.id}`}>
        <h3 id={`item-${item.id}`} className="text-lg font-semibold">
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className="underline underline-offset-4 hover:no-underline"
            >
              {item.title}
              <span className="sr-only">{t("opensInNewTab")}</span>
            </a>
          ) : (
            item.title
          )}
        </h3>
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          {href ? <span>{hostOf(href)}</span> : <span>{t("unlinked")}</span>}
          {published && item.published_at && (
            <time dateTime={item.published_at}>{t("published", { date: published })}</time>
          )}
          {reviewer && item.sources.length > 0 && (
            <span>
              {t("from", { sources: item.sources.map((source) => source.name).join(", ") })}
            </span>
          )}
        </p>
        {item.excerpt && <p className="mt-2">{item.excerpt}</p>}
        {reviewer && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Badge tone={item.status === "new" ? "plain" : "strong"}>
              {t(`status.${item.status}`)}
            </Badge>
            {reasonKey && <span className="text-sm">{t(`reasons.${reasonKey}`)}</span>}
            <div className="flex flex-wrap gap-2">
              {ACTIONS_FOR[item.status].map((action) => (
                <Button
                  key={action}
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  aria-label={t("actionOn", { action: t(`actions.${action}`), title: item.title })}
                  onClick={() => onAction(item, action)}
                >
                  {t(`actions.${action}`)}
                </Button>
              ))}
            </div>
          </div>
        )}
      </article>
    </li>
  );
}
