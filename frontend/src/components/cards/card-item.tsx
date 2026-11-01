"use client";

import { useLocale, useTranslations } from "next-intl";
import { useId, useState } from "react";
import { InsightPanel } from "@/components/cards/insight-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import { CATEGORIES } from "@/lib/items";
import { safeHref } from "@/lib/links";
import { type Card } from "@/lib/cards";

type Props<T extends Card> = {
  card: T;
  csrf: string;
  /** Administrators and editors also see the quality score. */
  reviewer: boolean;
  /** True while a step for this card is on its way. */
  busy: boolean;
  onSave: (card: T) => void;
  /** Resolves true when the backend took it, so the button shows only what is true. */
  onUseful: (card: T, on: boolean) => Promise<boolean>;
  onNotUseful: (card: T) => void;
  /** The person looked at the card closely (opened Try this or followed the link). */
  onSeen: (card: T) => void;
};

/**
 * One card. The title, the summary and the category come from a third party and from a model: they
 * are rendered as text, never as markup. The link is made only for an http(s) address that passes
 * `safeHref`, opens in a new tab with no opener and no referrer, and shows its host.
 */
export function CardItem<T extends Card>({
  card,
  csrf,
  reviewer,
  busy,
  onSave,
  onUseful,
  onNotUseful,
  onSeen,
}: Props<T>) {
  const t = useTranslations("Cards");
  const items = useTranslations("Items");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const [useful, setUseful] = useState(false);
  const [usefulPending, setUsefulPending] = useState(false);
  const panel = useId();
  const href = safeHref(card.url);
  const published = formatDate(card.published_at, locale);
  const category = (CATEGORIES as readonly string[]).includes(card.category)
    ? (card.category as (typeof CATEGORIES)[number])
    : null;

  return (
    <li className="rounded-lg border border-border p-4 break-words">
      <article aria-labelledby={`card-${card.id}`}>
        <h3 id={`card-${card.id}`} className="text-lg font-semibold">
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className="underline underline-offset-4 hover:no-underline"
              onClick={() => onSeen(card)}
            >
              {card.title}
              <span className="sr-only">{items("opensInNewTab")}</span>
            </a>
          ) : (
            card.title
          )}
        </h3>
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          {href ? <span>{new URL(href).host}</span> : <span>{items("unlinked")}</span>}
          {published && card.published_at && (
            <time dateTime={card.published_at}>{items("published", { date: published })}</time>
          )}
          {reviewer && typeof card.quality === "number" && (
            <span>{items("quality", { score: card.quality })}</span>
          )}
        </p>
        <p className="mt-2">{card.summary}</p>
        {category && (
          <p className="mt-2">
            <Badge tone="plain">{items(`categories.${category}`)}</Badge>
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            type="button"
            variant={open ? "default" : "outline"}
            size="sm"
            aria-expanded={open}
            aria-controls={panel}
            aria-label={t("actionOn", { action: t("tryThis"), title: card.title })}
            onClick={() => {
              if (!open) onSeen(card);
              setOpen(!open);
            }}
          >
            {t("tryThis")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            aria-pressed={card.saved}
            aria-label={t("actionOn", {
              action: card.saved ? t("saved") : t("save"),
              title: card.title,
            })}
            onClick={() => onSave(card)}
          >
            {card.saved ? t("saved") : t("save")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy || usefulPending}
            aria-pressed={useful}
            aria-label={t("actionOn", { action: t("useful"), title: card.title })}
            onClick={async () => {
              setUsefulPending(true);
              try {
                if (await onUseful(card, !useful)) setUseful(!useful);
              } finally {
                setUsefulPending(false);
              }
            }}
          >
            {t("useful")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            aria-label={t("actionOn", { action: t("notUseful"), title: card.title })}
            onClick={() => onNotUseful(card)}
          >
            {t("notUseful")}
          </Button>
        </div>
        <div id={panel} hidden={!open}>
          {open && <InsightPanel itemId={card.id} title={card.title} csrf={csrf} />}
        </div>
      </article>
    </li>
  );
}
