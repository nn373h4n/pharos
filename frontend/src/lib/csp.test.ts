import { describe, expect, it } from "vitest";
import { buildCsp, makeNonce } from "@/lib/csp";

const NONCE = "abc123==";

function directives(policy: string): Record<string, string[]> {
  return Object.fromEntries(
    policy.split("; ").map((part) => {
      const [name, ...values] = part.split(" ");
      return [name!, values];
    }),
  );
}

describe("the policy", () => {
  const policy = directives(buildCsp({ nonce: NONCE }));

  it("allows scripts only with the nonce of the request and what they load", () => {
    expect(policy["script-src"]).toEqual(["'self'", `'nonce-${NONCE}'`, "'strict-dynamic'"]);
    expect(policy["script-src"]).not.toContain("'unsafe-inline'");
    expect(policy["script-src"]).not.toContain("'unsafe-eval'");
  });

  it("lets styles be inline and nothing else", () => {
    expect(policy["style-src"]).toEqual(["'self'", "'unsafe-inline'"]);
    for (const name of ["default-src", "img-src", "font-src", "connect-src"]) {
      expect(policy[name]).not.toContain("'unsafe-inline'");
      expect(policy[name]).not.toContain("*");
    }
  });

  it("shuts the doors that are not needed", () => {
    expect(policy["object-src"]).toEqual(["'none'"]);
    expect(policy["frame-ancestors"]).toEqual(["'none'"]);
    expect(policy["base-uri"]).toEqual(["'self'"]);
    expect(policy["form-action"]).toEqual(["'self'"]);
    expect(policy["connect-src"]).toEqual(["'self'"]);
    expect(policy["worker-src"]).toEqual(["'self'"]);
    expect(policy["frame-src"]).toEqual(["'none'"]);
    expect(policy["manifest-src"]).toEqual(["'self'"]);
    expect(policy["upgrade-insecure-requests"]).toEqual([]);
  });

  it("upgrades insecure requests only for a visitor who came over https", () => {
    const plain = directives(buildCsp({ nonce: NONCE, secure: false }));
    expect("upgrade-insecure-requests" in plain).toBe(false);
    expect(plain["script-src"]).toEqual(policy["script-src"]); // nothing else changes
  });

  it("is looser in development only", () => {
    const dev = directives(buildCsp({ nonce: NONCE, development: true }));
    expect(dev["script-src"]).toContain("'unsafe-eval'");
    expect(dev["connect-src"]).toContain("ws:");
    expect("upgrade-insecure-requests" in dev).toBe(false);
  });
});

describe("the nonce", () => {
  it("is long, base64 and different every time", () => {
    const seen = new Set(Array.from({ length: 50 }, () => makeNonce()));
    expect(seen.size).toBe(50);
    for (const nonce of seen) expect(nonce).toMatch(/^[A-Za-z0-9+/]{22}==$/);
  });
});
