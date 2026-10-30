"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { FeedCard } from "@/components/feeds/feed-card";
import { FeedDeleteDialog } from "@/components/feeds/feed-delete-dialog";
import { FeedForm } from "@/components/feeds/feed-form";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ApiProblem } from "@/lib/api";
import {
  asSourceProblems,
  createFeed,
  deleteFeed,
  listFeeds,
  MAX_FEEDS,
  updateFeed,
  type Feed,
  type FeedPage,
  type NewFeed,
} from "@/lib/feeds";
import { problemKey, type ProblemKey } from "@/lib/problems";
import { problemsByField, type FieldKey, type FormField } from "@/lib/source-text";

type Loaded = { key: string; page: FeedPage } | { key: string; failed: ProblemKey };
type Notice = { kind: "done" | "error"; text: string };
type Problems = Partial<Record<FormField, FieldKey>>;
type FormProblems = { fields: Problems; request: ProblemKey | null };
type Editor = { feed?: Feed };

const NO_PROBLEMS: FormProblems = { fields: {}, request: null };

/**
 * The feeds of the signed-in person: up to five, seen by nobody else. Everything that changes
 * goes through one step at a time. The backend decides what is allowed; a person who is told the
 * feature is off, or that they have the most feeds, is told so by the backend's answer.
 */
export function FeedsView() {
  const t = useTranslations("Feeds");
  const s = useTranslations("Sources");
  const problems = useTranslations("Problems");
  const { state } = useSession();
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  // One step at a time: while it is sent nothing else on the screen can change what it applies to.
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [formProblems, setFormProblems] = useState<FormProblems>(NO_PROBLEMS);
  const [removing, setRemoving] = useState<Feed | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  // Asked for after a step; done once the dialogs are closed, as a modal page cannot take focus.
  const [focusAsked, setFocusAsked] = useState(0);

  const signedIn = state.status === "authenticated";
  const csrf = state.status === "authenticated" ? state.session.csrf_token : "";
  const key = String(reloads);

  useEffect(() => {
    if (!signedIn) return;
    let current = true;
    listFeeds().then(
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
  }, [key, signedIn]);

  useEffect(() => {
    if (focusAsked > 0) heading.current?.focus();
  }, [focusAsked]);

  if (!signedIn) return null;
  const settled = loaded !== null && loaded.key === key ? loaded : null;
  const page = settled && "page" in settled ? settled.page : null;
  const full = page !== null && page.total >= MAX_FEEDS;

  function failure(error: unknown): Notice {
    const problem = problemKey(error);
    const seconds = error instanceof ApiProblem ? error.retryAfter : null;
    return {
      kind: "error",
      text:
        problem === "rateLimited" && seconds === null
          ? problems("rateLimitedNoWait")
          : problems(problem, { seconds: seconds ?? 0 }),
    };
  }

  /** The list changed under a step: show it as it is now, and put focus where it will not be lost. */
  function refreshed(text: string) {
    setReloads((count) => count + 1);
    setNotice({ kind: "done", text });
    setFocusAsked((count) => count + 1);
  }

  async function toggle(feed: Feed) {
    setBusy(true);
    setNotice(null);
    try {
      await updateFeed(feed.id, { enabled: !feed.enabled }, csrf);
      refreshed(t(feed.enabled ? "done.disabled" : "done.enabled", { name: feed.name }));
    } catch (error) {
      setNotice(failure(error));
    } finally {
      setBusy(false);
    }
  }

  async function save(values: NewFeed) {
    const existing = editor?.feed;
    setBusy(true);
    setFormProblems(NO_PROBLEMS);
    try {
      if (existing) {
        await updateFeed(
          existing.id,
          { name: values.name, url: values.url, interval_seconds: values.interval_seconds },
          csrf,
        );
      } else {
        await createFeed(values, csrf);
      }
      setEditor(null);
      refreshed(t(existing ? "done.updated" : "done.created", { name: values.name }));
    } catch (error) {
      const fields =
        error instanceof ApiProblem ? problemsByField(asSourceProblems(error.fields)) : {};
      const problem = problemKey(error);
      setFormProblems({
        fields,
        // A refusal that names a field is shown at the field; any other is told for the whole form.
        request: problem === "validation" && Object.keys(fields).length > 0 ? null : problem,
      });
    } finally {
      setBusy(false);
    }
  }

  async function remove(feed: Feed) {
    setBusy(true);
    try {
      await deleteFeed(feed.id, csrf);
      setRemoving(null);
      refreshed(t("done.deleted", { name: feed.name }));
    } catch (error) {
      setRemoving(null);
      setNotice(failure(error));
      if (error instanceof ApiProblem && error.status === 404) setReloads((count) => count + 1);
    } finally {
      setBusy(false);
    }
  }

  const off = settled !== null && "failed" in settled && settled.failed === "notFound";

  return (
    <>
      <p className="mt-2 max-w-2xl text-muted-foreground">{t("intro")}</p>

      {!off && (
        <Button
          type="button"
          className="mt-6"
          disabled={busy || full}
          onClick={() => {
            setFormProblems(NO_PROBLEMS);
            setEditor({});
          }}
        >
          {t("actions.add")}
        </Button>
      )}
      {full && <p className="mt-2 text-sm">{t("limitReached", { max: MAX_FEEDS })}</p>}

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

      {off && (
        <p role="alert" className="mt-4 rounded-md border border-border bg-muted p-4">
          {t("off")}
        </p>
      )}
      {settled !== null && "failed" in settled && !off && (
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
              {s("retry")}
            </Button>
          )}
        </div>
      )}
      {page && page.items.length === 0 && (
        <p className="mt-4 text-muted-foreground">{t("empty")}</p>
      )}
      {page && page.items.length > 0 && (
        <>
          <p className="mt-2 text-sm text-muted-foreground">
            {t("used", { count: page.total, max: MAX_FEEDS })}
          </p>
          <ul className="mt-4 space-y-4">
            {page.items.map((feed) => (
              <FeedCard
                key={feed.id}
                feed={feed}
                busy={busy}
                onToggle={toggle}
                onEdit={(found) => {
                  setFormProblems(NO_PROBLEMS);
                  setEditor({ feed: found });
                }}
                onDelete={setRemoving}
              />
            ))}
          </ul>
        </>
      )}

      <Dialog
        open={editor !== null}
        onClose={() => !busy && setEditor(null)}
        titleId="feed-form-title"
      >
        {editor && (
          <FeedForm
            key={editor.feed?.id ?? "new"}
            feed={editor.feed}
            busy={busy}
            problems={formProblems.fields}
            requestProblem={formProblems.request}
            onSubmit={save}
            onCancel={() => setEditor(null)}
          />
        )}
      </Dialog>
      <Dialog
        open={removing !== null}
        onClose={() => !busy && setRemoving(null)}
        titleId="delete-title"
      >
        {removing && (
          <FeedDeleteDialog
            feed={removing}
            busy={busy}
            onConfirm={() => remove(removing)}
            onCancel={() => setRemoving(null)}
          />
        )}
      </Dialog>
    </>
  );
}
