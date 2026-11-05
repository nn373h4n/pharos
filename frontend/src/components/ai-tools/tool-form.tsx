"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/field";
import type { ProblemKey } from "@/lib/problems";
import {
  NAME_LIMIT,
  URL_LIMIT,
  type AiTool,
  type ToolField,
  type ToolFieldKey,
} from "@/lib/ai-tools";

export type ToolValues = { name: string; url: string };

type Props = {
  tool?: AiTool;
  busy: boolean;
  /** What the backend said is wrong with each entry. */
  problems: Partial<Record<ToolField, ToolFieldKey>>;
  /** A refusal that is about the request as a whole, not about one entry. */
  requestProblem: ProblemKey | null;
  onSubmit: (values: ToolValues) => void;
  onCancel: () => void;
};

/** One approved tool: a name people recognize and the address "Open in" goes to. */
export function ToolForm({ tool, busy, problems, requestProblem, onSubmit, onCancel }: Props) {
  const t = useTranslations("AiTools.form");
  const errors = useTranslations("AiTools.fieldErrors");
  const problemText = useTranslations("Problems");
  const ids = { name: useId(), url: useId() };
  const [name, setName] = useState(tool?.name ?? "");
  const [url, setUrl] = useState(tool?.url ?? "");

  return (
    <form
      noValidate
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit({ name: name.trim(), url: url.trim() });
      }}
    >
      <h2 id="ai-tool-form-title" className="text-xl font-semibold">
        {tool ? t("titleEdit") : t("titleNew")}
      </h2>
      <TextField
        id={ids.name}
        label={t("name")}
        hint={t("nameHint", { limit: NAME_LIMIT })}
        error={problems.name ? errors(problems.name) : undefined}
        value={name}
        maxLength={NAME_LIMIT}
        autoComplete="off"
        required
        disabled={busy}
        onChange={(event) => setName(event.target.value)}
      />
      <TextField
        id={ids.url}
        label={t("url")}
        hint={t("urlHint")}
        error={problems.url ? errors(problems.url) : undefined}
        value={url}
        maxLength={URL_LIMIT}
        type="url"
        inputMode="url"
        autoComplete="off"
        spellCheck={false}
        required
        disabled={busy}
        onChange={(event) => setUrl(event.target.value)}
      />
      {requestProblem && (
        <p role="alert" className="rounded-md border border-border bg-muted p-3">
          {problemText(requestProblem, { seconds: 0 })}
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
          {t("cancel")}
        </Button>
        <Button type="submit" disabled={busy}>
          {busy ? t("saving") : t("save")}
        </Button>
      </div>
    </form>
  );
}
