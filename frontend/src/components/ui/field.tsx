import * as React from "react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

type Common = {
  id: string;
  label: string;
  hint?: string;
  /** Why the entry is not valid. Shown under the field and tied to it for screen readers. */
  error?: string;
};

function describedBy(id: string, hint?: string, error?: string): string | undefined {
  const ids = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean);
  return ids.length > 0 ? ids.join(" ") : undefined;
}

function Frame({ id, label, hint, error, children }: Common & { children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      {children}
      {hint && (
        <p id={`${id}-hint`} className="text-sm text-muted-foreground">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-sm font-medium">
          {error}
        </p>
      )}
    </div>
  );
}

/** A labelled text entry with its hint and its problem, both read out with the field. */
export function TextField({
  id,
  label,
  hint,
  error,
  ...props
}: Common & Omit<React.ComponentProps<"input">, "id">) {
  return (
    <Frame id={id} label={label} hint={hint} error={error}>
      <Input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        {...props}
      />
    </Frame>
  );
}

/** A labelled choice from a short list, with the browser's own control. */
export function SelectField({
  id,
  label,
  hint,
  error,
  className,
  children,
  ...props
}: Common & Omit<React.ComponentProps<"select">, "id">) {
  return (
    <Frame id={id} label={label} hint={hint} error={error}>
      <select
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, hint, error)}
        className={cn(
          "h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-2",
          className,
        )}
        {...props}
      >
        {children}
      </select>
    </Frame>
  );
}
