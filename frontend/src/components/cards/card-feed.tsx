"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { CardItem } from "@/components/cards/card-item";
import { Button } from "@/components/ui/button";
import { ApiProblem } from "@/lib/api";
import {
  deleteInteraction,
  listCards,
  putInteraction,
  type Card,
  type CardPage,
} from "@/lib/cards";
import { problemKey, type ProblemKey } from "@/lib/problems";

/** A card with its place in the list as the server sent it, so a hidden card can come back there. */
type Ranked = Card & { rank: number };

type Loaded =
  | { phase: "loading" }
  | { phase: "failed"; problem: ProblemKey }
  | { phase: "ready"; cards: Ranked[]; next: string | null };

type Undo = { card: Ranked };

/**
 * The Home feed: the cards best for this person first, twenty at a time. What each button does is
 * sent to the backend, which keeps it private to the person. Hiding a card ("Not useful") can be
 * taken back until the next one.
 */
export function CardFeed() {
  const t = useTranslations("Cards");
  const problems = useTranslations("Problems");
  const { state } = useSession();
  const [loaded, setLoaded] = useState<Loaded>({ phase: "loading" });
  const [reloads, setReloads] = useState(0);
  const [more, setMore] = useState<"idle" | "loading">("idle");
  // Every card with a step on its way: two cards can be busy at once.
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const ranks = useRef(0);
  const [notice, setNotice] = useState("");
  const [undo, setUndo] = useState<Undo | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const role = state.status === "authenticated" ? state.session.user.role : null;
  const csrf = state.status === "authenticated" ? state.session.csrf_token : "";

  useEffect(() => {
    if (role === null) return;
    let current = true;
    listCards().then(
      (page: CardPage) => {
        if (!current) return;
        ranks.current = 0;
        setLoaded({
          phase: "ready",
          cards: page.items.map((c) => ({ ...c, rank: ranks.current++ })),
          next: page.next_cursor,
        });
      },
      (error) => {
        if (current) setLoaded({ phase: "failed", problem: problemKey(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [role, reloads]);

  function say(error: unknown) {
    const key = problemKey(error);
    const wait = error instanceof ApiProblem ? error.retryAfter : null;
    setNotice(
      key === "rateLimited" && wait === null
        ? problems("rateLimitedNoWait")
        : problems(key, { seconds: wait ?? 0 }),
    );
  }

  async function showMore() {
    if (loaded.phase !== "ready" || loaded.next === null) return;
    setMore("loading");
    try {
      const page = await listCards(loaded.next);
      setLoaded((now) =>
        now.phase === "ready"
          ? {
              phase: "ready",
              // A card is listed once, even if the list moved while it was read.
              cards: [
                ...now.cards,
                ...page.items
                  .filter((c) => !now.cards.some((known) => known.id === c.id))
                  .map((c) => ({ ...c, rank: ranks.current++ })),
              ],
              next: page.next_cursor,
            }
          : now,
      );
    } catch (error) {
      if (error instanceof ApiProblem && error.status === 422) {
        // The place in the list is too old: start again from the top.
        setNotice(t("refreshed"));
        setLoaded({ phase: "loading" });
        setReloads((n) => n + 1);
      } else {
        say(error);
      }
    } finally {
      setMore("idle");
    }
  }

  function update(id: string, change: Partial<Card>) {
    setLoaded((now) =>
      now.phase === "ready"
        ? { ...now, cards: now.cards.map((c) => (c.id === id ? { ...c, ...change } : c)) }
        : now,
    );
  }

  function working(id: string, on: boolean) {
    setBusy((now) => {
      const next = new Set(now);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function save(card: Card) {
    working(card.id, true);
    try {
      if (card.saved) await deleteInteraction(card.id, "save", csrf);
      else await putInteraction(card.id, "save", csrf);
      update(card.id, { saved: !card.saved });
      setNotice(t(card.saved ? "unsavedDone" : "savedDone", { title: card.title }));
    } catch (error) {
      say(error);
    } finally {
      working(card.id, false);
    }
  }

  async function useful(card: Card, on: boolean): Promise<boolean> {
    try {
      if (on) await putInteraction(card.id, "useful", csrf);
      else await deleteInteraction(card.id, "useful", csrf);
      setNotice(t(on ? "usefulDone" : "usefulUndone", { title: card.title }));
      return true;
    } catch (error) {
      say(error);
      return false;
    }
  }

  async function notUseful(card: Ranked) {
    working(card.id, true);
    try {
      await putInteraction(card.id, "not_useful", csrf);
      // Built from the list as it is now, not as it was when the button was pressed.
      setLoaded((now) =>
        now.phase === "ready" ? { ...now, cards: now.cards.filter((c) => c.id !== card.id) } : now,
      );
      setUndo({ card });
      setNotice(t("hidden", { title: card.title }));
      heading.current?.focus();
    } catch (error) {
      say(error);
    } finally {
      working(card.id, false);
    }
  }

  async function undoHide() {
    if (undo === null) return;
    const { card } = undo;
    try {
      await deleteInteraction(card.id, "not_useful", csrf);
      setLoaded((now) =>
        now.phase === "ready" && !now.cards.some((c) => c.id === card.id)
          ? { ...now, cards: [...now.cards, card].sort((a, b) => a.rank - b.rank) }
          : now,
      );
      setUndo(null);
      setNotice(t("restored", { title: card.title }));
    } catch (error) {
      say(error);
    }
  }

  function seen(card: Card) {
    // Only the fact that the person looked closely; a failure here is not worth interrupting for.
    void putInteraction(card.id, "view", csrf).catch(() => undefined);
  }

  if (role === null) return null;

  return (
    <section aria-labelledby="cards-heading" className="mt-10">
      <h2 id="cards-heading" ref={heading} tabIndex={-1} className="text-2xl font-semibold">
        {t("results")}
      </h2>
      <p role="status" className="mt-2 min-h-5 text-sm">
        {notice}
      </p>
      {undo && (
        <Button type="button" variant="outline" size="sm" className="mb-3" onClick={undoHide}>
          {t("undo")}
        </Button>
      )}
      {loaded.phase === "loading" && <p className="mt-4 text-muted-foreground">{t("loading")}</p>}
      {loaded.phase === "failed" && (
        <div className="mt-4">
          <p role="alert">
            {loaded.problem === "forbidden" || loaded.problem === "rateLimited"
              ? problems(loaded.problem, { seconds: 60 })
              : t("loadFailed")}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => {
              setLoaded({ phase: "loading" });
              setReloads((n) => n + 1);
            }}
          >
            {t("retry")}
          </Button>
        </div>
      )}
      {loaded.phase === "ready" && loaded.cards.length === 0 && (
        <p className="mt-4">{t("empty")}</p>
      )}
      {loaded.phase === "ready" && loaded.cards.length > 0 && (
        <>
          <ul className="mt-4 space-y-4" aria-label={t("results")}>
            {loaded.cards.map((card) => (
              <CardItem
                key={card.id}
                card={card}
                csrf={csrf}
                reviewer={role === "admin" || role === "editor"}
                busy={busy.has(card.id)}
                onSave={save}
                onUseful={useful}
                onNotUseful={notUseful}
                onSeen={seen}
              />
            ))}
          </ul>
          {loaded.next !== null && (
            <Button
              type="button"
              variant="outline"
              className="mt-4"
              disabled={more === "loading"}
              onClick={showMore}
            >
              {more === "loading" ? t("loadingMore") : t("showMore")}
            </Button>
          )}
        </>
      )}
    </section>
  );
}
