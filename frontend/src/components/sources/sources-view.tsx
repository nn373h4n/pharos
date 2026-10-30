"use client";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { DeleteDialog } from "@/components/sources/delete-dialog";
import { SourceCard } from "@/components/sources/source-card";
import { SourceForm, type FormValues } from "@/components/sources/source-form";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { SelectField } from "@/components/ui/field";
import { ApiProblem } from "@/lib/api";
import { problemKey, type ProblemKey } from "@/lib/problems";
import { problemsByField, type FieldKey, type FormField } from "@/lib/source-text";
import {
  createSource,
  deleteSource,
  listSources,
  PAGE_SIZE,
  SOURCE_TYPES,
  updateSource,
  type Source,
  type SourcePage,
  type SourceType,
} from "@/lib/sources";

type Loaded = { key: string; page: SourcePage } | { key: string; failed: ProblemKey };
type Notice = { kind: "done" | "error"; text: string };
type Problems = Partial<Record<FormField, FieldKey>>;
type FormProblems = { fields: Problems; request: ProblemKey | null };
type Editor = { source?: Source };

const NO_PROBLEMS: FormProblems = { fields: {}, request: null };

/**
 * The sources of the organization. Administrators add, change, switch on and off, try and delete;
 * editors only look. What each person may do is decided by the backend: a page opened by someone
 * without the right shows the refusal, not data.
 */
export function SourcesView() {
  const t = useTranslations("Sources");
  const problems = useTranslations("Problems");
  const locale = useLocale();
  const { state } = useSession();
  const [type, setType] = useState<SourceType | "">("");
  const [switched, setSwitched] = useState<"" | "on" | "off">("");
  const [offset, setOffset] = useState(0);
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  // One step at a time: while it is sent nothing else on the screen can change what it applies to.
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [formProblems, setFormProblems] = useState<FormProblems>(NO_PROBLEMS);
  const [removing, setRemoving] = useState<Source | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  // Asked for after a step; done once the dialogs are closed, as a modal page cannot take focus.
  const [focusAsked, setFocusAsked] = useState(0);

  const role = state.status === "authenticated" ? state.session.user.role : null;
  const csrf = state.status === "authenticated" ? state.session.csrf_token : "";
  const admin = role === "admin";
  const key = `${type}|${switched}|${offset}|${reloads}`;

  useEffect(() => {
    if (role === null) return;
    let current = true;
    listSources({
      type: type === "" ? undefined : type,
      enabled: switched === "" ? undefined : switched === "on",
      offset,
    }).then(
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
  }, [key, type, switched, offset, role]);

  useEffect(() => {
    if (focusAsked > 0) heading.current?.focus();
  }, [focusAsked]);

  if (role === null) return null;
  const settled = loaded !== null && loaded.key === key ? loaded : null;
  const page = settled && "page" in settled ? settled.page : null;

  function filter(change: () => void) {
    change();
    setOffset(0);
    setNotice(null);
  }

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
  function refreshed(text: string, backAPage = false) {
    if (backAPage && offset > 0) setOffset(Math.max(0, offset - PAGE_SIZE));
    else setReloads((count) => count + 1);
    setNotice({ kind: "done", text });
    setFocusAsked((count) => count + 1);
  }

  async function toggle(source: Source) {
    setBusy(true);
    setNotice(null);
    try {
      await updateSource(source.id, { enabled: !source.enabled }, csrf);
      refreshed(t(source.enabled ? "done.disabled" : "done.enabled", { name: source.name }));
    } catch (error) {
      setNotice(failure(error));
    } finally {
      setBusy(false);
    }
  }

  async function save(values: FormValues) {
    const existing = editor?.source;
    setBusy(true);
    setFormProblems(NO_PROBLEMS);
    try {
      if (existing) {
        await updateSource(
          existing.id,
          { name: values.name, config: values.config, interval_seconds: values.interval },
          csrf,
        );
      } else {
        await createSource(
          {
            type: values.type,
            name: values.name,
            config: values.config,
            interval_seconds: values.interval,
            enabled: values.enabled,
          },
          csrf,
        );
        setOffset(0);
      }
      setEditor(null);
      refreshed(t(existing ? "done.updated" : "done.created", { name: values.name }));
    } catch (error) {
      const fields = error instanceof ApiProblem ? problemsByField(error.fields) : {};
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

  async function remove(source: Source) {
    setBusy(true);
    try {
      await deleteSource(source.id, csrf);
      setRemoving(null);
      const last = page !== null && page.items.length === 1;
      refreshed(t("done.deleted", { name: source.name }), last);
    } catch (error) {
      setRemoving(null);
      setNotice(failure(error));
      if (error instanceof ApiProblem && error.status === 404) setReloads((count) => count + 1);
    } finally {
      setBusy(false);
    }
  }

  const from = page && page.total > 0 ? offset + 1 : 0;
  const to = page ? offset + page.items.length : 0;
  const emptyNow = page !== null && page.items.length === 0;
  const filtered = type !== "" || switched !== "";

  return (
    <>
      <p className="mt-2 max-w-2xl text-muted-foreground">{t("intro")}</p>
      {!admin && <p className="mt-2 max-w-2xl text-sm">{t("readOnly")}</p>}

      {admin && (
        <Button
          type="button"
          className="mt-6"
          disabled={busy}
          onClick={() => {
            setFormProblems(NO_PROBLEMS);
            setEditor({});
          }}
        >
          {t("actions.add")}
        </Button>
      )}

      <div role="group" aria-label={t("filters.label")} className="mt-6 grid gap-4 sm:grid-cols-2">
        <SelectField
          id="filter-type"
          label={t("filters.type")}
          value={type}
          disabled={busy}
          onChange={(event) => filter(() => setType(event.target.value as SourceType | ""))}
        >
          <option value="">{t("filters.allTypes")}</option>
          {SOURCE_TYPES.map((option) => (
            <option key={option} value={option}>
              {t(`types.${option}`)}
            </option>
          ))}
        </SelectField>
        <SelectField
          id="filter-state"
          label={t("filters.state")}
          value={switched}
          disabled={busy}
          onChange={(event) => filter(() => setSwitched(event.target.value as "" | "on" | "off"))}
        >
          <option value="">{t("filters.allStates")}</option>
          <option value="on">{t("filters.on")}</option>
          <option value="off">{t("filters.off")}</option>
        </SelectField>
      </div>

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
      {emptyNow && (
        <p className="mt-4 text-muted-foreground">
          {filtered || !admin ? t("empty") : t("emptyAdmin")}
        </p>
      )}
      {page && page.items.length > 0 && (
        <>
          <p className="mt-2 text-sm text-muted-foreground">{t("total", { total: page.total })}</p>
          <ul className="mt-4 space-y-4">
            {page.items.map((source) => (
              <SourceCard
                key={source.id}
                source={source}
                admin={admin}
                csrf={csrf}
                busy={busy}
                onToggle={toggle}
                onEdit={(found) => {
                  setFormProblems(NO_PROBLEMS);
                  setEditor({ source: found });
                }}
                onDelete={setRemoving}
              />
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

      <Dialog
        open={editor !== null}
        onClose={() => !busy && setEditor(null)}
        titleId="source-form-title"
      >
        {editor && (
          <SourceForm
            key={editor.source?.id ?? "new"}
            source={editor.source}
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
          <DeleteDialog
            source={removing}
            busy={busy}
            onConfirm={() => remove(removing)}
            onCancel={() => setRemoving(null)}
          />
        )}
      </Dialog>
    </>
  );
}
