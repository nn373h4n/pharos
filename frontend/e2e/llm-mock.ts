import type { Request, Route } from "@playwright/test";

// The model-layer part of the mocked API (docs/API.md, "Model layer"). Only administrators may
// ask; the key check is answered by the "worker" on the second read.

export type LlmOptions = {
  llmProvider?: "off" | "anthropic";
  llmWarned?: boolean;
  llmExhausted?: boolean;
  llmFail?: "down";
};

export type LlmState = { keyReads: number };

const TEST = "7b2e1d3f-4c5e-4f60-9bac-1d2e3f4a5b6c";

type Problem = (
  status: number,
  code: string,
  headers?: Record<string, string>,
) => Parameters<Route["fulfill"]>[0];

export async function handleLlm(
  route: Route,
  request: Request,
  path: string,
  state: LlmState,
  options: LlmOptions,
  role: "admin" | "editor" | "member",
  problem: Problem,
): Promise<boolean> {
  if (!path.startsWith("/api/llm")) return false;
  if (role !== "admin") {
    await route.fulfill(problem(403, "forbidden"));
    return true;
  }
  if (path === "/api/llm/status") {
    if (options.llmFail === "down") {
      await route.fulfill(problem(503, "service_unavailable"));
      return true;
    }
    await route.fulfill({
      json: {
        provider: options.llmProvider ?? "anthropic",
        models: { filter: "small-model", enrichment: "big-model", insight: "" },
        daily_cap_micro_usd: 2_000_000,
        spent_micro_usd: options.llmExhausted ? 2_000_000 : options.llmWarned ? 1_700_000 : 500_000,
        warned: options.llmWarned ?? false,
        exhausted: options.llmExhausted ?? false,
        key_check: { outcome: "ok", checked_at: "2026-10-31T12:00:00Z" },
        usage: [
          {
            day: "2026-10-31",
            stage: "enrichment",
            calls: 12,
            refused: 1,
            failed: 0,
            tokens_in: 4200,
            tokens_out: 900,
            cost_micro_usd: 31000,
          },
        ],
      },
    });
    return true;
  }
  if (path === "/api/llm/key-test" && request.method() === "POST") {
    state.keyReads = 0;
    await route.fulfill({ status: 202, json: { test_id: TEST, retry_after_seconds: 60 } });
    return true;
  }
  if (path === `/api/llm/key-test/${TEST}`) {
    state.keyReads += 1;
    await route.fulfill({
      json:
        state.keyReads < 2
          ? { status: "pending", outcome: null, checked_at: null }
          : { status: "done", outcome: "ok", checked_at: "2026-10-31T12:05:00Z" },
    });
    return true;
  }
  await route.fulfill(problem(404, "not_found"));
  return true;
}
