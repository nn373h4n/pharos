// The insight burst (M5-08, D5). Run by scripts/loadtest.sh with LOAD_SCENARIO=insights.
//
// Every person opens "Try this" on a card at about the same time (the starts are spread over RAMP
// seconds). The first answer is `pending` (the worker is asked); the person then looks again every
// POLL seconds until the insight is `ready` or `unavailable`. What this measures: the API never
// answers with a server error while the queue is full, and the worker (with the stack's fake
// provider: no model, no key) drains it. Each person asks for another article.
import http from "k6/http";
import { sleep } from "k6";
import { SharedArray } from "k6/data";
import { Counter, Trend } from "k6/metrics";

const sessions = new SharedArray("sessions", () =>
  open("/work/sessions.txt").split("\n").filter((line) => line.length > 0),
);
const items = new SharedArray("items", () =>
  open("/work/items.txt").split("\n").filter((line) => line.length > 0),
);
const BASE = __ENV.BASE_URL;
const COOKIE = __ENV.COOKIE_NAME;
const RAMP = Number(__ENV.RAMP || 20);
const POLL = Number(__ENV.POLL || 3);
const GIVE_UP_SECONDS = Number(__ENV.GIVE_UP || 300);

const answers = {
  ok: new Counter("answers_2xx"),
  limited: new Counter("answers_429"),
  server: new Counter("answers_5xx"),
  other: new Counter("answers_other"),
};
const finished = {
  ready: new Counter("insights_ready"),
  unavailable: new Counter("insights_unavailable"),
  gave_up: new Counter("insights_gave_up"),
};
const secondsToFinish = new Trend("insight_seconds_to_finish", true);

function count(response) {
  const status = response.status;
  if (status >= 200 && status < 300) answers.ok.add(1);
  else if (status === 429) answers.limited.add(1);
  else if (status >= 500) answers.server.add(1);
  else answers.other.add(1); // 0 is a timeout or a refused connection
}

export const options = {
  insecureSkipTLSVerify: true,
  scenarios: {
    burst: {
      executor: "per-vu-iterations",
      vus: Number(__ENV.VUS || 300),
      iterations: 1,
      maxDuration: `${GIVE_UP_SECONDS + RAMP + 60}s`,
    },
  },
  thresholds: { "http_req_duration{name:insight}": ["p(95)<1000000"] },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export default function () {
  sleep(Math.random() * RAMP);
  const headers = { Cookie: `${COOKIE}=${sessions[(__VU - 1) % sessions.length]}` };
  const item = items[(__VU - 1) % items.length];
  const began = Date.now();
  while ((Date.now() - began) / 1000 < GIVE_UP_SECONDS) {
    const response = http.get(`${BASE}/api/items/${item}/insight`, {
      headers,
      tags: { name: "insight" },
    });
    count(response);
    if (response.status === 200) {
      const status = response.json("status");
      if (status === "ready" || status === "unavailable") {
        (status === "ready" ? finished.ready : finished.unavailable).add(1);
        secondsToFinish.add(Date.now() - began);
        return;
      }
    }
    sleep(POLL);
  }
  finished.gave_up.add(1);
}
