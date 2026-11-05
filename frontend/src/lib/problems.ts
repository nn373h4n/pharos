import { ApiProblem } from "@/lib/api";

/** Keys of the `Problems` messages: what a failed call is called for the person who sees it. */
export type ProblemKey =
  | "forbidden"
  | "notFound"
  | "itemState"
  | "rateLimited"
  | "sourceConflict"
  | "sourceLimit"
  | "aiToolConflict"
  | "aiToolLimit"
  | "validation"
  | "unavailable"
  | "csrf"
  | "generic";

/** Which message fits a failed call. The code is the backend's, never shown as it is. */
export function problemKey(error: unknown): ProblemKey {
  if (!(error instanceof ApiProblem)) return "generic";
  if (error.status === 0 || error.status === 503 || error.code === "service_unavailable") {
    return "unavailable";
  }
  switch (error.code) {
    case "forbidden":
      return "forbidden";
    case "not_found":
      return "notFound";
    case "item_state":
      return "itemState";
    case "rate_limited":
      return "rateLimited";
    case "source_conflict":
      return "sourceConflict";
    case "source_limit":
      return "sourceLimit";
    case "ai_tool_conflict":
      return "aiToolConflict";
    case "ai_tool_limit":
      return "aiToolLimit";
    case "validation_failed":
      return "validation";
    case "csrf_failed":
      return "csrf";
    default:
      return "generic";
  }
}
