"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { useSession } from "@/components/auth/session-provider";
import { ToolDeleteDialog } from "@/components/ai-tools/tool-delete-dialog";
import { ToolForm, type ToolValues } from "@/components/ai-tools/tool-form";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import {
  MAX_TOOLS,
  createAiTool,
  deleteAiTool,
  listAiTools,
  problemsByField,
  toolHref,
  updateAiTool,
  type AiTool,
  type ToolField,
  type ToolFieldKey,
} from "@/lib/ai-tools";
import { ApiProblem } from "@/lib/api";
import { problemKey, type ProblemKey } from "@/lib/problems";

type Loaded = { reloads: number; tools: AiTool[] } | { reloads: number; failed: ProblemKey };
type Notice = { kind: "done" | "error"; text: string };
type FormProblems = {
  fields: Partial<Record<ToolField, ToolFieldKey>>;
  request: ProblemKey | null;
};
type Editor = { tool?: AiTool };

const NO_PROBLEMS: FormProblems = { fields: {}, request: null };

/**
 * The AI tools that are approved for "Open in" on a card. Administrators add, change and delete
 * them; everybody else may only look. The list is empty until an administrator adds a tool, and a
 * card then offers "Copy" only. What each person may do is decided by the backend.
 */
export function AiToolsView() {
  const t = useTranslations("AiTools");
  const problems = useTranslations("Problems");
  const { state } = useSession();
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  // One step at a time: while it is sent nothing else on the screen can change what it applies to.
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [formProblems, setFormProblems] = useState<FormProblems>(NO_PROBLEMS);
  const [removing, setRemoving] = useState<AiTool | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  // Asked for after a step; done once the dialogs are closed, as a modal page cannot take focus.
  const [focusAsked, setFocusAsked] = useState(0);

  const role = state.status === "authenticated" ? state.session.user.role : null;
  const csrf = state.status === "authenticated" ? state.session.csrf_token : "";
  const admin = role === "admin";

  useEffect(() => {
    if (role === null) return;
    let current = true;
    listAiTools().then(
      (tools) => {
        if (current) setLoaded({ reloads, tools });
      },
      (error) => {
        if (current) setLoaded({ reloads, failed: problemKey(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [reloads, role]);

  useEffect(() => {
    if (focusAsked > 0) heading.current?.focus();
  }, [focusAsked]);

  if (role === null) return null;
  const settled = loaded !== null && loaded.reloads === reloads ? loaded : null;
  const tools = settled && "tools" in settled ? settled.tools : null;

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

  async function save(values: ToolValues) {
    const existing = editor?.tool;
    setBusy(true);
    setFormProblems(NO_PROBLEMS);
    try {
      if (existing) await updateAiTool(existing.id, values, csrf);
      else await createAiTool(values, csrf);
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

  async function remove(tool: AiTool) {
    setBusy(true);
    try {
      await deleteAiTool(tool.id, csrf);
      setRemoving(null);
      refreshed(t("done.deleted", { name: tool.name }));
    } catch (error) {
      setRemoving(null);
      setNotice(failure(error));
      if (error instanceof ApiProblem && error.status === 404) setReloads((count) => count + 1);
    } finally {
      setBusy(false);
    }
  }

  const full = tools !== null && tools.length >= MAX_TOOLS;

  return (
    <>
      <p className="mt-2 max-w-2xl text-muted-foreground">{t("intro")}</p>
      {!admin && <p className="mt-2 max-w-2xl text-sm">{t("readOnly")}</p>}

      {admin && (
        <>
          <Button
            type="button"
            className="mt-6"
            disabled={busy || full}
            aria-describedby={full ? "ai-tools-full" : undefined}
            onClick={() => {
              setFormProblems(NO_PROBLEMS);
              setEditor({});
            }}
          >
            {t("actions.add")}
          </Button>
          {full && (
            <p id="ai-tools-full" className="mt-2 text-sm text-muted-foreground">
              {t("full", { max: MAX_TOOLS })}
            </p>
          )}
        </>
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
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={() => setReloads((count) => count + 1)}
          >
            {t("retry")}
          </Button>
        </div>
      )}
      {tools !== null && tools.length === 0 && (
        <p className="mt-4 text-muted-foreground">{admin ? t("emptyAdmin") : t("empty")}</p>
      )}
      {tools !== null && tools.length > 0 && (
        <>
          <p className="mt-2 text-sm text-muted-foreground">
            {t("count", { count: tools.length, max: MAX_TOOLS })}
          </p>
          <ul className="mt-4 space-y-4">
            {tools.map((tool) => {
              const href = toolHref(tool.url);
              return (
                <li key={tool.id} className="rounded-lg border border-border p-4">
                  <h3 className="break-words font-semibold">{tool.name}</h3>
                  <p className="mt-1 break-all text-sm text-muted-foreground">
                    {href ? (
                      <>
                        <span className="sr-only">{t("addressLabel")} </span>
                        {tool.url}
                      </>
                    ) : (
                      t("addressRefused")
                    )}
                  </p>
                  {admin && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        aria-label={t("actions.actionOn", {
                          action: t("actions.edit"),
                          name: tool.name,
                        })}
                        onClick={() => {
                          setFormProblems(NO_PROBLEMS);
                          setEditor({ tool });
                        }}
                      >
                        {t("actions.edit")}
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        aria-label={t("actions.actionOn", {
                          action: t("actions.delete"),
                          name: tool.name,
                        })}
                        onClick={() => setRemoving(tool)}
                      >
                        {t("actions.delete")}
                      </Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}

      <Dialog
        open={editor !== null}
        onClose={() => !busy && setEditor(null)}
        titleId="ai-tool-form-title"
      >
        {editor && (
          <ToolForm
            key={editor.tool?.id ?? "new"}
            tool={editor.tool}
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
        titleId="ai-tool-delete-title"
      >
        {removing && (
          <ToolDeleteDialog
            tool={removing}
            busy={busy}
            onConfirm={() => remove(removing)}
            onCancel={() => setRemoving(null)}
          />
        )}
      </Dialog>
    </>
  );
}
