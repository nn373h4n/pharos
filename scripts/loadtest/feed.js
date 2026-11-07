// The Home feed under load (M5-08, D5). Run by scripts/loadtest.sh in a digest-pinned k6 container.
//
// Each virtual user is a person who opens the feed, reads the first page, goes on to the second and
// pauses (THINK seconds) before the next visit. People are taken in turn from the sessions file, so
// the 300 synthetic people share the virtual users.
import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";

const sessions = new SharedArray("sessions", () =>
  open("/work/sessions.txt").split("\n").filter((line) => line.length > 0),
);
const BASE = __ENV.BASE_URL;
const COOKIE = __ENV.COOKIE_NAME;
const THINK = Number(__ENV.THINK || 1);

export const options = {
  insecureSkipTLSVerify: true, // the throwaway stack uses Caddy's own certificate authority
  scenarios: {
    feed: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 50),
      duration: __ENV.DURATION || "10m",
    },
  },
  thresholds: {
    "http_req_duration{name:feed_first}": ["p(95)<400"],
    "http_req_duration{name:feed_next}": ["p(95)<400"],
    http_req_failed: ["rate<0.01"],
    checks: ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export default function () {
  const person = sessions[(__VU * 7919 + __ITER) % sessions.length];
  const params = { headers: { Cookie: `${COOKIE}=${person}` }, tags: { name: "feed_first" } };
  const first = http.get(`${BASE}/api/cards?limit=20`, params);
  const ok = check(first, {
    "first page is 200": (r) => r.status === 200,
    "first page is JSON": (r) => r.headers["Content-Type"].includes("json"),
  });
  if (ok) {
    const cursor = first.json("next_cursor");
    if (cursor) {
      const next = http.get(`${BASE}/api/cards?limit=20&cursor=${encodeURIComponent(cursor)}`, {
        headers: params.headers,
        tags: { name: "feed_next" },
      });
      check(next, { "next page is 200": (r) => r.status === 200 });
    }
  }
  sleep(THINK);
}
