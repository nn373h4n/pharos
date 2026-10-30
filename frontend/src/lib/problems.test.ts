import { describe, expect, it } from "vitest";
import { ApiProblem } from "@/lib/api";
import { problemKey } from "@/lib/problems";

describe("problemKey", () => {
  it.each([
    [new ApiProblem(403, "forbidden"), "forbidden"],
    [new ApiProblem(404, "not_found"), "notFound"],
    [new ApiProblem(409, "item_state"), "itemState"],
    [new ApiProblem(429, "rate_limited", 12), "rateLimited"],
    [new ApiProblem(403, "csrf_failed"), "csrf"],
    [new ApiProblem(503, "service_unavailable"), "unavailable"],
    [new ApiProblem(503, "anything"), "unavailable"],
    [new ApiProblem(0, "network_error"), "unavailable"],
    [new ApiProblem(500, "internal_error"), "generic"],
    [new ApiProblem(422, "validation_failed"), "validation"],
    [new ApiProblem(409, "source_conflict"), "sourceConflict"],
    [new ApiProblem(409, "source_limit"), "sourceLimit"],
    [new ApiProblem(409, "something_new"), "generic"],
    [new Error("boom"), "generic"],
    ["text", "generic"],
    [null, "generic"],
  ])("names %j", (error, key) => {
    expect(problemKey(error)).toBe(key);
  });
});
