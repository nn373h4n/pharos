"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/utils";

type Props = {
  open: boolean;
  onClose: () => void;
  /** The id of the element inside that names the dialog. */
  titleId: string;
  children: ReactNode;
  className?: string;
};

/**
 * A modal dialog made with the browser's own `<dialog>`: it traps focus, makes the rest of the page
 * inert, closes with Escape and gives focus back to what opened it. Its content is built only
 * while it is open, so a form inside it starts empty every time.
 */
export function Dialog({ open, onClose, titleId, children, className }: Props) {
  const element = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = element.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={element}
      aria-labelledby={titleId}
      onClose={() => {
        // A second Escape cannot be cancelled: the browser closes the dialog itself. If the
        // parent still wants it open (a save is on its way), bring it back.
        const dialog = element.current;
        if (open && dialog && !dialog.open) dialog.showModal();
      }}
      onCancel={(event) => {
        // Escape: let the parent decide, the effect above closes the dialog.
        event.preventDefault();
        onClose();
      }}
      className={cn(
        "m-auto w-[min(34rem,calc(100%-2rem))] max-h-[calc(100%-2rem)] overflow-y-auto rounded-lg border border-border bg-background p-6 text-foreground backdrop:bg-black/60",
        className,
      )}
    >
      {open ? children : null}
    </dialog>
  );
}
