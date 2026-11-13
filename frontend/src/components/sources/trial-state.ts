import type { ProblemKey } from "@/lib/problems";
import type { TestAnswer } from "@/lib/sources";

/** Where one trial fetch of a source stands; kept by the list of sources (F-29). */
export type TrialState =
  | { phase: "idle" }
  | { phase: "starting" }
  // When the trial was asked for: the page gives up two minutes after it, however often the
  // card was shown again in between.
  | { phase: "waiting"; testId: string; startedAt: number }
  | { phase: "answer"; answer: TestAnswer }
  | { phase: "gaveUp" }
  | { phase: "failed"; problem: ProblemKey; seconds: number | null };

export const IDLE: TrialState = { phase: "idle" };
