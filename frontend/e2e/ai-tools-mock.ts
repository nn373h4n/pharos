import type { Request, Route } from "@playwright/test";

// The AI tools part of the mocked API (docs/API.md, "AI tools"): everybody signed in reads the
// list, only administrators change it. The checks are a small copy of the backend's: https only,
// no credentials, no IP address, the same name once, ten at most.

export type MockTool = { id: string; name: string; url: string };

export type AiToolsOptions = { aiTools?: MockTool[] };

export type AiToolsState = { aiTools: MockTool[] };

type Problem = (
  status: number,
  code: string,
  headers?: Record<string, string>,
) => Parameters<Route["fulfill"]>[0];

export const tool = (n: number, changes: Partial<MockTool> = {}): MockTool => ({
  id: `50000000-0000-4000-8000-0000000000${String(n).padStart(2, "0")}`,
  name: `Tool ${n}`,
  url: `https://tool${n}.example.org/chat`,
  ...changes,
});

export function defaultAiTools(): MockTool[] {
  return [tool(1), tool(2), tool(3)];
}

function urlProblem(url: string): string | null {
  if (!url.startsWith("https://")) return "https_only";
  if (url.includes("@")) return "credentials_not_allowed";
  if (/^https:\/\/(\d{1,3}\.){3}\d{1,3}([:/?]|$)/.test(url)) return "ip_address_not_allowed";
  if (/[\s#]/.test(url)) return "invalid_url";
  return null;
}

export async function handleAiTools(
  route: Route,
  request: Request,
  path: string,
  state: AiToolsState,
  role: "admin" | "editor" | "member",
  problem: Problem,
): Promise<boolean> {
  if (!path.startsWith("/api/ai-tools")) return false;
  if (path === "/api/ai-tools" && request.method() === "GET") {
    await route.fulfill({
      json: { items: [...state.aiTools].sort((a, b) => a.name.localeCompare(b.name)) },
    });
    return true;
  }
  if (role !== "admin") {
    await route.fulfill(problem(403, "forbidden"));
    return true;
  }
  const invalid = (field: string, type: string) =>
    route.fulfill({
      status: 422,
      contentType: "application/problem+json",
      body: JSON.stringify({
        code: "validation_failed",
        status: 422,
        errors: [{ loc: ["body", field], type }],
      }),
    });
  const taken = (name: string, except?: string) =>
    state.aiTools.some((t) => t.id !== except && t.name.toLowerCase() === name.toLowerCase());
  if (path === "/api/ai-tools" && request.method() === "POST") {
    const body = request.postDataJSON() as { name: string; url: string };
    const wrong = urlProblem(body.url);
    if (wrong) return (await invalid("url", wrong), true);
    if (taken(body.name)) return (await route.fulfill(problem(409, "ai_tool_conflict")), true);
    if (state.aiTools.length >= 10)
      return (await route.fulfill(problem(409, "ai_tool_limit")), true);
    const created = tool(state.aiTools.length + 20, { name: body.name, url: body.url });
    state.aiTools.push(created);
    await route.fulfill({ status: 201, json: created });
    return true;
  }
  const found = state.aiTools.find((t) => path === `/api/ai-tools/${t.id}`);
  if (!found) {
    await route.fulfill(problem(404, "not_found"));
    return true;
  }
  if (request.method() === "PATCH") {
    const body = request.postDataJSON() as { name?: string; url?: string };
    const wrong = body.url === undefined ? null : urlProblem(body.url);
    if (wrong) return (await invalid("url", wrong), true);
    if (body.name !== undefined && taken(body.name, found.id)) {
      return (await route.fulfill(problem(409, "ai_tool_conflict")), true);
    }
    Object.assign(found, body);
    await route.fulfill({ json: found });
    return true;
  }
  if (request.method() === "DELETE") {
    state.aiTools = state.aiTools.filter((t) => t.id !== found.id);
    await route.fulfill({ status: 204 });
    return true;
  }
  await route.fulfill(problem(405, "method_not_allowed"));
  return true;
}
