import * as React from "react";
import { cn } from "@/lib/utils";

type Tone = "plain" | "strong";

/** A short label for a state. Colour alone never carries the meaning: the label is always text. */
export function Badge({
  tone = "plain",
  className,
  ...props
}: React.ComponentProps<"span"> & { tone?: Tone }) {
  return (
    <span
      data-slot="badge"
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
        tone === "strong"
          ? "border-primary bg-primary text-primary-foreground"
          : "border-border bg-muted text-foreground",
        className,
      )}
      {...props}
    />
  );
}
