// The fixed words of the sources API, turned into keys of the translated messages. The backend
// sends words, never sentences; a word this page does not know becomes "other", never raw text.

import type { FieldProblem } from "@/lib/api";

export const OUTCOMES = [
  "running",
  "ok",
  "not_modified",
  "blocked",
  "timeout",
  "too_large",
  "bad_status",
  "bad_content",
  "network",
  "error",
  "host_busy",
  "source_missing",
] as const;
export type OutcomeKey = (typeof OUTCOMES)[number] | "other";

export function outcomeKey(value: string | null): OutcomeKey {
  return (OUTCOMES as readonly string[]).includes(value ?? "") ? (value as OutcomeKey) : "other";
}

export const ERROR_WORDS = [
  "http_error",
  "not_allowed",
  "redirects",
  "timed_out",
  "too_large",
  "unexpected",
  "unreachable",
  "unreadable",
  "json_invalid",
  "json_shape",
  "bad_config",
  "config_invalid",
  "store_failed",
  "unsupported",
  "unsupported_type",
  "bad_content",
  "too_complex",
] as const;
export type ErrorKey = (typeof ERROR_WORDS)[number] | "other";

export function errorKey(value: string): ErrorKey {
  return (ERROR_WORDS as readonly string[]).includes(value) ? (value as ErrorKey) : "other";
}

/** The parts of the form a problem can belong to. */
export type FormField = "name" | "config" | "interval" | "form";

export type FieldKey =
  | "urlRequired"
  | "urlInvalid"
  | "urlNotAllowed"
  | "repoInvalid"
  | "queryInvalid"
  | "pointsInvalid"
  | "nameInvalid"
  | "nameMissing"
  | "nameTooLong"
  | "intervalInvalid"
  | "configInvalid"
  | "formInvalid";

/** Which part of the form a problem is about, from where the backend says it is. */
export function fieldOf(problem: FieldProblem): FormField {
  const where = problem.loc[problem.loc.length - 1];
  if (where === "name") return "name";
  if (where === "config") return "config";
  if (where === "interval_seconds") return "interval";
  return "form";
}

/** Which message tells a person what is wrong with this part of the form. */
export function fieldKey(problem: FieldProblem): FieldKey {
  const type = problem.type;
  switch (fieldOf(problem)) {
    case "name":
      if (type === "invalid_text") return "nameInvalid";
      if (type === "string_too_long") return "nameTooLong";
      return "nameMissing";
    case "interval":
      return "intervalInvalid";
    case "config":
      switch (type) {
        case "url_required":
          return "urlRequired";
        case "url_invalid":
          return "urlInvalid";
        case "url_not_allowed":
          return "urlNotAllowed";
        case "repo_required":
        case "repo_invalid":
          return "repoInvalid";
        case "query_required":
        case "query_invalid":
          return "queryInvalid";
        case "min_points_invalid":
          return "pointsInvalid";
        default:
          return "configInvalid";
      }
    default:
      return "formInvalid";
  }
}

/** What is wrong, by part of the form. The first problem of a part is the one that is shown. */
export function problemsByField(problems: FieldProblem[]): Partial<Record<FormField, FieldKey>> {
  const found: Partial<Record<FormField, FieldKey>> = {};
  for (const problem of problems) {
    const field = fieldOf(problem);
    if (found[field] === undefined) found[field] = fieldKey(problem);
  }
  return found;
}
