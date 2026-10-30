"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { SelectField, TextField } from "@/components/ui/field";
import {
  DEFAULT_FEED_INTERVAL,
  FEED_INTERVALS,
  MAX_URL,
  type Feed,
  type NewFeed,
} from "@/lib/feeds";
import type { ProblemKey } from "@/lib/problems";
import { type FieldKey, type FormField } from "@/lib/source-text";
import { MAX_NAME } from "@/lib/sources";

type Problems = Partial<Record<FormField, FieldKey>>;

type Props = {
  /** The feed being changed; absent when a new one is added. */
  feed?: Feed;
  busy: boolean;
  /** What the backend said was wrong, by part of the form (the address is called `config`). */
  problems: Problems;
  /** A problem with the request as a whole (a duplicate, the limit, the service). */
  requestProblem: ProblemKey | null;
  onSubmit: (values: NewFeed) => void;
  onCancel: () => void;
};

/** What can be checked before asking: something is there. */
function checkHere(name: string, url: string): Problems {
  const found: Problems = {};
  if (name.trim() === "") found.name = "nameMissing";
  if (url.trim() === "") found.config = "urlRequired";
  return found;
}

/** The form to add a feed or to change one. Used inside a dialog. */
export function FeedForm({ feed, busy, problems, requestProblem, onSubmit, onCancel }: Props) {
  const t = useTranslations("Feeds");
  const s = useTranslations("Sources");
  const failures = useTranslations("Problems");
  const editing = feed !== undefined;
  const [name, setName] = useState(feed?.name ?? "");
  const [url, setUrl] = useState(feed?.url ?? "");
  const [interval, setInterval] = useState<number>(feed?.interval_seconds ?? DEFAULT_FEED_INTERVAL);
  const [enabled, setEnabled] = useState(true);
  const [local, setLocal] = useState<Problems>({});
  const summary = useRef<HTMLDivElement>(null);

  const shown: Problems = { ...problems, ...local };
  const summaryKey = JSON.stringify([shown, requestProblem]);
  const anything = Object.keys(shown).length > 0 || requestProblem !== null;

  // When something is wrong, the person is taken to the summary at the top of the form.
  useEffect(() => {
    if (summaryKey !== JSON.stringify([{}, null])) summary.current?.focus();
  }, [summaryKey]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const found = checkHere(name, url);
    setLocal(found);
    if (Object.keys(found).length > 0) return;
    onSubmit({ name: name.trim(), url: url.trim(), interval_seconds: interval, enabled });
  }

  const message = (key: FieldKey | undefined) => (key ? s(`fieldErrors.${key}`) : undefined);
  const intervals: readonly number[] = FEED_INTERVALS.includes(interval as never)
    ? FEED_INTERVALS
    : [interval, ...FEED_INTERVALS];

  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <h2 id="feed-form-title" className="text-xl font-semibold">
        {editing ? t("form.editTitle") : t("form.addTitle")}
      </h2>

      <div
        ref={summary}
        tabIndex={-1}
        role={anything ? "alert" : undefined}
        className={anything ? "rounded-md border-2 border-border bg-muted p-3 text-sm" : undefined}
      >
        {anything && (
          <>
            <p>{s("form.problems")}</p>
            {requestProblem && <p className="mt-1">{failures(requestProblem, { seconds: 0 })}</p>}
          </>
        )}
      </div>

      <TextField
        id="feed-name"
        label={s("form.name")}
        hint={s("form.nameHint")}
        error={message(shown.name)}
        value={name}
        maxLength={MAX_NAME}
        autoComplete="off"
        onChange={(event) => {
          setName(event.target.value);
          setLocal({});
        }}
      />
      <TextField
        id="feed-url"
        label={s("form.url")}
        hint={t("form.urlHint")}
        error={message(shown.config)}
        value={url}
        type="url"
        inputMode="url"
        maxLength={MAX_URL}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => {
          setUrl(event.target.value);
          setLocal({});
        }}
      />
      <SelectField
        id="feed-interval"
        label={s("form.interval")}
        error={message(shown.interval)}
        value={interval}
        onChange={(event) => setInterval(Number(event.target.value))}
      >
        {intervals.map((seconds) => (
          <option key={seconds} value={seconds}>
            {(FEED_INTERVALS as readonly number[]).includes(seconds)
              ? s(`intervals.${seconds as (typeof FEED_INTERVALS)[number]}`)
              : s("intervalOther", { seconds })}
          </option>
        ))}
      </SelectField>

      {!editing && (
        <div className="flex items-center gap-2">
          <input
            id="feed-enabled"
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            className="h-5 w-5"
          />
          <label htmlFor="feed-enabled" className="text-sm font-medium">
            {t("form.followNow")}
          </label>
        </div>
      )}

      <div className="flex flex-wrap justify-end gap-2 pt-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
          {s("form.cancel")}
        </Button>
        <Button type="submit" disabled={busy}>
          {busy ? s("form.saving") : s("form.save")}
        </Button>
      </div>
    </form>
  );
}
