import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiProblem, fieldProblems, request } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

describe("fieldProblems", () => {
  it("reads the documented shape", () => {
    expect(
      fieldProblems({ errors: [{ loc: ["body", "config"], type: "url_not_allowed" }] }),
    ).toEqual([{ loc: ["body", "config"], type: "url_not_allowed" }]);
  });
  it("turns numbers in a path into text", () => {
    expect(fieldProblems({ errors: [{ loc: ["body", 0], type: "missing" }] })).toEqual([
      { loc: ["body", "0"], type: "missing" },
    ]);
  });
  it.each([
    null,
    "text",
    5,
    {},
    { errors: "x" },
    { errors: [null, 5, "x", []] },
    { errors: [{ loc: "body", type: "x" }] },
    { errors: [{ loc: ["body"], type: 5 }] },
    { errors: [{ loc: [{}], type: "x" }] },
    { errors: [{ type: "x" }] },
  ])("keeps nothing from %j", (value) => {
    expect(fieldProblems(value)).toEqual([]);
  });
  it("keeps at most twenty", () => {
    const errors = Array.from({ length: 50 }, () => ({ loc: ["body"], type: "x" }));
    expect(fieldProblems({ errors })).toHaveLength(20);
  });
});

describe("a refused call", () => {
  it("carries the field problems, never the values", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              code: "validation_failed",
              errors: [{ loc: ["body", "config"], type: "url_invalid", input: "secret" }],
            }),
            { status: 422 },
          ),
      ),
    );
    const error = await request("/api/x").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiProblem);
    const problem = error as ApiProblem;
    expect([problem.status, problem.code]).toEqual([422, "validation_failed"]);
    expect(problem.fields).toEqual([{ loc: ["body", "config"], type: "url_invalid" }]);
    expect(JSON.stringify(problem.fields)).not.toContain("secret");
  });
  it("has no field problems when the answer is not a problem document", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })),
    );
    const error = (await request("/api/x").catch((caught: unknown) => caught)) as ApiProblem;
    expect([error.status, error.code, error.fields]).toEqual([502, "http_error", []]);
  });
});
