"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { ItemCard } from "@/components/items/item-card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { cn } from "@/lib/utils";
import { ApiProblem } from "@/lib/api";
import {
  actOnItem,
  listItems,
  PAGE_SIZE,
  RESULT_OF,
  type Item,
  type ItemAction,
  type ItemPage,
  type ItemStatus,
} from "@/lib/items";
import { problemKey, type ProblemKey } from "@/lib/problems";

const STATUSES: ItemStatus[] = ["new", "held", "hidden"];

type Loaded = { key: string; page: ItemPage } | { key: string; failed: ProblemKey };
type Notice = { kind: "done" | "error"; text: string };

/**
 * The articles, newest first. Everyone reads the current ones; administrators and editors can also
 * look at the held and hidden ones and review (hold, approve, hide, restore). What is shown is
 * decided by the backend: a member who asks for more gets a refusal, not data.
 */
export function ItemsView({ sourceId }: { sourceId?: string }) {
  const t = useTranslations("Items");
  const problems = useTranslations("Problems");
  const locale = useLocale();
  const { state } = useSession();
  const [status, setStatus] = useState<ItemStatus>("new");
  const [offset, setOffset] = useState(0);
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  // One step at a time: while it is sent, nothing else on the screen can change what the answer
  // would be applied to (the other buttons, the filters and the pager are all off).
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  const role = state.status === "authenticated" ? state.session.user.role : null;
  const csrf = state.status === "authenticated" ? state.session.csrf_token : "";
  const reviewer = role === "admin" || role === "editor";
  const key = `${status}|${offset}|${reloads}|${sourceId ?? ""}`;

  useEffect(() => {
    if (role === null) return;
    let current = true;
    listItems({ status, offset, sourceId }).then(
      (page) => {
        if (current) setLoaded({ key, page });
      },
      (error) => {
        if (current) setLoaded({ key, failed: problemKey(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [key, status, offset, role, sourceId]);

  if (role === null) return null;
  const settled = loaded !== null && loaded.key === key ? loaded : null;

  function show(next: ItemStatus) {
    setStatus(next);
    setOffset(0);
    setNotice(null);
  }

  async function act(item: Item, action: ItemAction) {
    setBusy(true);
    setNotice(null);
    try {
      await actOnItem(item.id, action, csrf);
      const shown = settled && "page" in settled ? settled.page : null;
      if (shown && RESULT_OF[action] !== status) {
        // The article has left this list.
        const items = shown.items.filter((found) => found.id !== item.id);
        if (items.length === 0 && offset > 0) {
          setOffset(Math.max(0, offset - PAGE_SIZE)); // do not leave an empty page behind
        } else {
          setLoaded((previous) =>
            previous && previous.key === key && "page" in previous
              ? { key, page: { items, total: shown.total - 1 } }
              : previous,
          );
        }
      } else {
        setReloads((count) => count + 1);
      }
      setNotice({ kind: "done", text: t(`done.${action}`, { title: item.title }) });
      heading.current?.focus();
    } catch (error) {
      const problem = problemKey(error);
      const seconds = error instanceof ApiProblem ? error.retryAfter : null;
      setNotice({
        kind: "error",
        text:
          problem === "rateLimited" && seconds === null
            ? problems("rateLimitedNoWait")
            : problems(problem, { seconds: seconds ?? 0 }),
      });
      // Someone else got there first: show what the list looks like now. The button that was
      // pressed goes away with the old list, so focus goes to the list's heading.
      if (problem === "itemState" || problem === "notFound") {
        setReloads((count) => count + 1);
        heading.current?.focus();
      }
    } finally {
      setBusy(false);
    }
  }

  const page = settled && "page" in settled ? settled.page : null;
  const from = page && page.total > 0 ? offset + 1 : 0;
  const to = page ? offset + page.items.length : 0;

  return (
    <>
      <p className="mt-2 max-w-2xl text-muted-foreground">{t("intro")}</p>
      {sourceId && (
        <p className="mt-4 flex flex-wrap items-center gap-3 rounded-md border border-border bg-muted p-3 text-sm">
          <span>{t("source.only")}</span>
          <Link href="/items" className={cn(buttonVariants({ variant: "outline", size: "sm" }))}>
            {t("source.all")}
          </Link>
        </p>
      )}

      {reviewer && (
        <div role="group" aria-label={t("filter.label")} className="mt-6 flex flex-wrap gap-2">
          {STATUSES.map((option) => (
            <Button
              key={option}
              type="button"
              size="sm"
              variant={option === status ? "default" : "outline"}
              aria-pressed={option === status}
              disabled={busy}
              onClick={() => show(option)}
            >
              {t(`filter.${option}`)}
            </Button>
          ))}
        </div>
      )}

      <h2 ref={heading} tabIndex={-1} className="mt-8 text-xl font-semibold">
        {t("results")}
      </h2>
      <div role="status" className="mt-2 min-h-6 text-muted-foreground">
        {settled === null && t("loading")}
        {notice?.kind === "done" && (
          <p className="rounded-md border border-border bg-muted p-3 text-foreground">
            {notice.text}
          </p>
        )}
      </div>
      {notice?.kind === "error" && (
        <p role="alert" className="mt-2 rounded-md border border-border bg-muted p-3">
          {notice.text}
        </p>
      )}

      {settled !== null && "failed" in settled && (
        <div role="alert" className="mt-4 rounded-md border border-border bg-muted p-4">
          <p>
            {settled.failed === "generic"
              ? t("loadFailed")
              : problems(settled.failed, { seconds: 0 })}
          </p>
          {settled.failed !== "forbidden" && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              onClick={() => setReloads((count) => count + 1)}
            >
              {t("retry")}
            </Button>
          )}
        </div>
      )}
      {page && page.items.length === 0 && (
        <p className="mt-4 text-muted-foreground">{t(`empty.${status}`)}</p>
      )}
      {page && page.items.length > 0 && (
        <>
          <p className="mt-2 text-sm text-muted-foreground">{t("total", { total: page.total })}</p>
          <ul className="mt-4 space-y-4">
            {page.items.map((item) => (
              <ItemCard key={item.id} item={item} reviewer={reviewer} busy={busy} onAction={act} />
            ))}
          </ul>
          <nav aria-label={t("pagination")} className="mt-6 flex flex-wrap items-center gap-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy || offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              {t("previous")}
            </Button>
            <p className="text-sm text-muted-foreground" lang={locale}>
              {t("range", { from, to, total: page.total })}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy || to >= page.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              {t("next")}
            </Button>
          </nav>
        </>
      )}
    </>
  );
}
