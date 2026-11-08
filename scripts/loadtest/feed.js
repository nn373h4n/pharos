// The Home feed under load (M5-08, D5). Run by scripts/loadtest.sh in a digest-pinned k6 container.
//
// One virtual user is one person, kept for the whole run (the sessions are taken in turn from the
// file, so up to as many people as sessions). A person opens the feed, reads the first page, goes on
// to the second page one time in two, and pauses (THINK seconds) before the next visit. That is the
// model of a person at work, and it stays inside the product's own limit of 300 feed requests an
// hour for one person (about 1.5 requests every 30 s is 180 an hour). Hammering the feed with no
// pause would only measure that limit.
import http from "k6/http";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";
import { Counter } from "k6/metrics";

const sessions = new SharedArray("sessions", () =>
  open("/work/sessions.txt").split("\n").filter((line) => line.length > 0),
);
// What the answers were, so that a failure has a cause and not only a number.
const answers = {
  ok: new Counter("answers_2xx"),
  limited: new Counter("answers_429"),
  server: new Counter("answers_5xx"),
  other: new Counter("answers_other"),
};
function count(response) {
  const status = response.status;
  if (status >= 200 && status < 300) answers.ok.add(1);
  else if (status === 429) answers.limited.add(1);
  else if (status >= 500) answers.server.add(1);
  else answers.other.add(1); // 0 is a timeout or a refused connection
}
const BASE = __ENV.BASE_URL;
const COOKIE = __ENV.COOKIE_NAME;
const THINK = Number(__ENV.THINK || 30);

export const options = {
  insecureSkipTLSVerify: true, // the throwaway stack uses Caddy's own certificate authority
  scenarios: {
    feed: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 50),
      duration: __ENV.DURATION || "10m",
    },
  },
  // k6 writes the timing of a tagged request to its summary only if a threshold names it. These
  // never fail; the verdict is worked out from the numbers by report.py.
  thresholds: {
    "http_req_duration{name:feed_first}": ["p(95)<1000000"],
    "http_req_duration{name:feed_next}": ["p(95)<1000000"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export default function () {
  // People do not arrive together: the first visit of each starts at a random moment of the first
  // pause, so the run does not open with all of them at once.
  if (__ITER === 0) sleep(Math.random() * THINK);
  const person = sessions[(__VU - 1) % sessions.length];
  const headers = { Cookie: `${COOKIE}=${person}` };
  const first = http.get(`${BASE}/api/cards?limit=20`, { headers, tags: { name: "feed_first" } });
  count(first);
  const ok = check(first, {
    "first page is 200": (r) => r.status === 200,
    "first page is JSON": (r) => (r.headers["Content-Type"] || "").includes("json"),
  });
  if (ok && Math.random() < 0.5) {
    const cursor = first.json("next_cursor");
    if (cursor) {
      const next = http.get(`${BASE}/api/cards?limit=20&cursor=${encodeURIComponent(cursor)}`, {
        headers,
        tags: { name: "feed_next" },
      });
      count(next);
      check(next, { "next page is 200": (r) => r.status === 200 });
    }
  }
  // People do not all start together: spread the pause so the visits do not come in waves.
  sleep(THINK * (0.7 + Math.random() * 0.6));
}
