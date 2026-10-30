"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState, type SyntheticEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { listFeedItems, PAGE_SIZE, type Feed, type FeedItemPage } from "@/lib/feeds";
import { formatDate } from "@/lib/format";
import { hostOf, safeHref } from "@/lib/links";

type Loaded = { key: string; page: FeedItemPage } | { key: string; failed: true };

const REASONS = ["link_ip_host", "link_odd_port", "instruction_phrase", "editor_hold"] as const;

/**
 * The articles a feed brought, for its owner only, loaded when the section is opened. Titles and
 * excerpts are text from a third party and are rendered as text; a link is made only for an
 * http(s) address. Nobody reviews them: a rule's flag is shown as a note to the owner.
 */
export function FeedArticles({ feed }: { feed: Feed }) {
  const t = useTranslations("Feeds.articles");
  const i = useTranslations("Items");
  const locale = useLocale();
  const [opened, setOpened] = useState(false);
  const [offset, setOffset] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const key = `${feed.id}|${offset}`;

  useEffect(() => {
    if (!opened) return;
    let current = true;
    listFeedItems(feed.id, offset).then(
      (page) => {
        if (current) setLoaded({ key, page });
      },
      () => {
        if (current) setLoaded({ key, failed: true });
      },
    );
    return () => {
      current = false;
    };
  }, [opened, feed.id, offset, key]);

  function toggled(event: SyntheticEvent<HTMLDetailsElement>) {
    if (event.currentTarget.open) setOpened(true);
  }

  const settled = loaded !== null && loaded.key === key ? loaded : null;
  const page = settled && "page" in settled ? settled.page : null;
  const to = page ? offset + page.items.length : 0;

  return (
    <details onToggle={toggled} className="mt-3">
      <summary className="cursor-pointer text-sm font-medium">{t("title")}</summary>
      <div className="mt-2" aria-live="polite">
        {opened && settled === null && (
          <p className="text-sm text-muted-foreground">{t("loading")}</p>
        )}
        {settled !== null && "failed" in settled && <p className="text-sm">{t("failed")}</p>}
        {page && page.items.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("empty")}</p>
        )}
        {page && page.items.length > 0 && (
          <>
            <ul className="space-y-3">
              {page.items.map((item) => {
                const href = safeHref(item.url);
                const published = formatDate(item.published_at, locale);
                return (
                  <li key={item.id} className="break-words rounded-md border border-border p-3">
                    <p className="font-medium">
                      {href ? (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer nofollow"
                          className="underline underline-offset-4 hover:no-underline"
                        >
                          {item.title}
                          <span className="sr-only">{i("opensInNewTab")}</span>
                        </a>
                      ) : (
                        item.title
                      )}
                    </p>
                    <p className="mt-1 flex flex-wrap gap-x-3 text-sm text-muted-foreground">
                      {href ? <span>{hostOf(href)}</span> : <span>{i("unlinked")}</span>}
                      {published && item.published_at && (
                        <time dateTime={item.published_at}>
                          {i("published", { date: published })}
                        </time>
                      )}
                    </p>
                    {item.excerpt && <p className="mt-1 text-sm">{item.excerpt}</p>}
                    {item.status !== "new" && (
                      <p className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                        <Badge tone="strong">{t("flagged")}</Badge>
                        {item.hold_reason && (
                          <span>
                            {i(
                              `reasons.${
                                (REASONS as readonly string[]).includes(item.hold_reason)
                                  ? (item.hold_reason as (typeof REASONS)[number])
                                  : "other"
                              }`,
                            )}
                          </span>
                        )}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
            <nav aria-label={t("pagination")} className="mt-3 flex flex-wrap items-center gap-3">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                {i("previous")}
              </Button>
              <p className="text-sm text-muted-foreground">
                {i("range", { from: offset + 1, to, total: page.total })}
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={to >= page.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                {i("next")}
              </Button>
            </nav>
          </>
        )}
      </div>
    </details>
  );
}
