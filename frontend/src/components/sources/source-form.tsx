"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { SelectField, TextField } from "@/components/ui/field";
import type { ProblemKey } from "@/lib/problems";
import { type FieldKey, type FormField } from "@/lib/source-text";
import {
  configOf,
  DEFAULT_INTERVAL,
  fieldsOf,
  INTERVALS,
  MAX_NAME,
  MAX_POINTS,
  MAX_QUERY,
  NO_FIELDS,
  SOURCE_TYPES,
  type ConfigFields,
  type Source,
  type SourceType,
} from "@/lib/sources";

export type FormValues = {
  type: SourceType;
  name: string;
  config: Record<string, unknown>;
  interval: number;
  enabled: boolean;
};

type Problems = Partial<Record<FormField, FieldKey>>;

type Props = {
  /** The source being changed; absent when a new one is added. */
  source?: Source;
  busy: boolean;
  /** What the backend said was wrong, by part of the form. */
  problems: Problems;
  /** A problem with the request as a whole (a duplicate, the limit, the service). */
  requestProblem: ProblemKey | null;
  onSubmit: (values: FormValues) => void;
  onCancel: () => void;
};

/** What can be checked before asking: something is there, and a number is a number. */
function checkHere(type: SourceType, name: string, fields: ConfigFields): Problems {
  const found: Problems = {};
  if (name.trim() === "") found.name = "nameMissing";
  if (type === "rss" && fields.url.trim() === "") found.config = "urlRequired";
  if (type === "github" && fields.repo.trim() === "") found.config = "repoInvalid";
  if (type === "hackernews") {
    const points = fields.points.trim();
    if (fields.query.trim() === "") found.config = "queryInvalid";
    else if (points !== "" && !(/^\d{1,5}$/.test(points) && Number(points) <= MAX_POINTS)) {
      found.config = "pointsInvalid";
    }
  }
  return found;
}

/** The form to add a source or to change one. Used inside a dialog. */
export function SourceForm({ source, busy, problems, requestProblem, onSubmit, onCancel }: Props) {
  const t = useTranslations("Sources");
  const failures = useTranslations("Problems");
  const editing = source !== undefined;
  const [type, setType] = useState<SourceType>(source?.type ?? "rss");
  const [name, setName] = useState(source?.name ?? "");
  const [fields, setFields] = useState<ConfigFields>(source ? fieldsOf(source) : NO_FIELDS);
  const [interval, setInterval] = useState<number>(source?.interval_seconds ?? DEFAULT_INTERVAL);
  const [enabled, setEnabled] = useState(false);
  const [local, setLocal] = useState<Problems>({});
  const summary = useRef<HTMLDivElement>(null);

  const shown: Problems = { ...problems, ...local };
  const summaryKey = JSON.stringify([shown, requestProblem]);
  const anything = Object.keys(shown).length > 0 || requestProblem !== null;

  // When something is wrong, the person is taken to the summary at the top of the form.
  useEffect(() => {
    if (summaryKey !== JSON.stringify([{}, null])) summary.current?.focus();
  }, [summaryKey]);

  function change(key: keyof ConfigFields, value: string) {
    setFields((previous) => ({ ...previous, [key]: value }));
    setLocal({});
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const found = checkHere(type, name, fields);
    setLocal(found);
    if (Object.keys(found).length > 0) return;
    onSubmit({ type, name: name.trim(), config: configOf(type, fields), interval, enabled });
  }

  const message = (key: FieldKey | undefined) => (key ? t(`fieldErrors.${key}`) : undefined);

  return (
    <form onSubmit={submit} noValidate className="space-y-4">
      <h2 id="source-form-title" className="text-xl font-semibold">
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
            <p>{t("form.problems")}</p>
            {requestProblem && <p className="mt-1">{failures(requestProblem, { seconds: 0 })}</p>}
          </>
        )}
      </div>

      <SelectField
        id="source-type"
        label={t("form.type")}
        hint={editing ? t("form.typeFixed") : undefined}
        value={type}
        disabled={editing}
        onChange={(event) => {
          setType(event.target.value as SourceType);
          setFields(NO_FIELDS);
          setLocal({});
        }}
      >
        {SOURCE_TYPES.map((option) => (
          <option key={option} value={option}>
            {t(`types.${option}`)}
          </option>
        ))}
      </SelectField>

      <TextField
        id="source-name"
        label={t("form.name")}
        hint={t("form.nameHint")}
        error={message(shown.name)}
        value={name}
        maxLength={MAX_NAME}
        autoComplete="off"
        onChange={(event) => {
          setName(event.target.value);
          setLocal({});
        }}
      />

      {type === "rss" && (
        <TextField
          id="source-url"
          label={t("form.url")}
          hint={t("form.urlHint")}
          error={message(shown.config)}
          value={fields.url}
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => change("url", event.target.value)}
        />
      )}
      {type === "github" && (
        <TextField
          id="source-repo"
          label={t("form.repo")}
          hint={t("form.repoHint")}
          error={message(shown.config)}
          value={fields.repo}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => change("repo", event.target.value)}
        />
      )}
      {type === "hackernews" && (
        <>
          <TextField
            id="source-query"
            label={t("form.query")}
            hint={t("form.queryHint")}
            error={shown.config === "pointsInvalid" ? undefined : message(shown.config)}
            value={fields.query}
            maxLength={MAX_QUERY}
            autoComplete="off"
            onChange={(event) => change("query", event.target.value)}
          />
          <TextField
            id="source-points"
            label={t("form.points")}
            hint={t("form.pointsHint")}
            error={shown.config === "pointsInvalid" ? message(shown.config) : undefined}
            value={fields.points}
            inputMode="numeric"
            autoComplete="off"
            onChange={(event) => change("points", event.target.value)}
          />
        </>
      )}

      <SelectField
        id="source-interval"
        label={t("form.interval")}
        error={message(shown.interval)}
        value={interval}
        onChange={(event) => setInterval(Number(event.target.value))}
      >
        {INTERVALS.map((seconds) => (
          <option key={seconds} value={seconds}>
            {t(`intervals.${seconds}`)}
          </option>
        ))}
      </SelectField>

      {!editing && (
        <div className="flex items-center gap-2">
          <input
            id="source-enabled"
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            className="h-5 w-5"
          />
          <label htmlFor="source-enabled" className="text-sm font-medium">
            {t("form.enabledNow")}
          </label>
        </div>
      )}

      <div className="flex flex-wrap justify-end gap-2 pt-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
          {t("form.cancel")}
        </Button>
        <Button type="submit" disabled={busy}>
          {busy ? t("form.saving") : t("form.save")}
        </Button>
      </div>
    </form>
  );
}
