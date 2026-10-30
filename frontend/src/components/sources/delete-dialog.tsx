"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import type { Source } from "@/lib/sources";

type Props = { source: Source; busy: boolean; onConfirm: () => void; onCancel: () => void };

/** The question before a source is deleted. "Cancel" has focus: the safe answer is the default. */
export function DeleteDialog({ source, busy, onConfirm, onCancel }: Props) {
  const t = useTranslations("Sources.deleteDialog");
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancel.current?.focus();
  }, []);

  return (
    <div className="space-y-4">
      <h2 id="delete-title" className="text-xl font-semibold">
        {t("title")}
      </h2>
      <p className="break-words">{t("body", { name: source.name })}</p>
      <div className="flex flex-wrap justify-end gap-2">
        <Button ref={cancel} type="button" variant="outline" onClick={onCancel} disabled={busy}>
          {t("cancel")}
        </Button>
        <Button type="button" onClick={onConfirm} disabled={busy}>
          {busy ? t("deleting") : t("confirm")}
        </Button>
      </div>
    </div>
  );
}
