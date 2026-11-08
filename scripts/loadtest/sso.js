// The single sign-on burst (M5-08, D5). Run by scripts/loadtest.sh with LOAD_SCENARIO=sso.
//
// Every person signs in through the development Keycloak, as a browser does: start at Pharos, fill
// in Keycloak's form, come back to the callback, then ask who I am. The starts are spread evenly
// over RAMP seconds (default 100), all from this one address, so the whole run can be judged against
// "100 sign-ins in 2 minutes": the last person must be signed in before DEADLINE seconds (120).
// What this measures: Keycloak, the callback (code exchange, key check, user creation, session) and
// the per-address limit of the sign-in endpoints, together. A person counts as signed in only when
// the callback sets the session cookie AND /api/me answers with their own name.
import http from "k6/http";
import exec from "k6/execution";
import { sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL;
const COOKIE = __ENV.COOKIE_NAME;
const PASSWORD = __ENV.SSO_PASSWORD;
const AUTH = __ENV.BASE_URL.replace("://localhost", "://auth.localhost");
const RAMP = Number(__ENV.RAMP || 100);
const VUS = Number(__ENV.VUS || 100);

const answers = {
  ok: new Counter("answers_2xx"),
  redirect: new Counter("answers_3xx"),
  limited: new Counter("answers_429"),
  server: new Counter("answers_5xx"),
  other: new Counter("answers_other"),
};
const outcome = {
  ok: new Counter("signins_ok"),
  failed: new Counter("signins_failed"),
};
const failedAt = {
  start: new Counter("signins_failed_at_start"),
  form: new Counter("signins_failed_at_keycloak_form"),
  submit: new Counter("signins_failed_at_keycloak_submit"),
  callback: new Counter("signins_failed_at_callback"),
  me: new Counter("signins_failed_at_me"),
};
const signinMs = new Trend("signin_ms", true);
const doneAtMs = new Trend("signin_done_at_ms", true); // since the start of the burst

function count(response) {
  const status = response.status;
  if (status >= 200 && status < 300) answers.ok.add(1);
  else if (status >= 300 && status < 400) answers.redirect.add(1);
  else if (status === 429) answers.limited.add(1);
  else if (status >= 500) answers.server.add(1);
  else answers.other.add(1); // 0 is a timeout or a refused connection
  return response;
}

const STEPS = ["start", "form", "submit", "callback", "me"];
const slow = Object.fromEntries(STEPS.map((name) => [`http_req_duration{name:${name}}`, ["p(95)<1000000"]]));

export const options = {
  insecureSkipTLSVerify: true,
  // k6 does not look up *.localhost by itself; both names are this machine (host networking).
  hosts: { "auth.localhost": "127.0.0.1", localhost: "127.0.0.1" },
  scenarios: {
    burst: {
      executor: "per-vu-iterations",
      vus: VUS,
      iterations: 1,
      maxDuration: `${RAMP + 300}s`,
    },
  },
  thresholds: slow,
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// Pharos answers 303 (See Other) and Keycloak 302; both mean "go to the Location".
const redirects = (response) => response.status === 302 || response.status === 303;

function fail(step) {
  failedAt[step].add(1);
  outcome.failed.add(1);
}

export default function () {
  const number = String(__VU).padStart(3, "0");
  const username = `load-${number}`;
  sleep(((__VU - 1) / VUS) * RAMP);
  const began = Date.now();

  // 1. Pharos sends the browser to Keycloak (PKCE, state and nonce are in the address).
  const start = count(http.get(`${BASE}/api/auth/oidc/login?next=/feed`, { redirects: 0, tags: { name: "start" } }));
  const authUrl = start.headers.Location;
  if (!redirects(start) || !authUrl || !authUrl.includes("/protocol/openid-connect/auth?")) return fail("start");

  // 2. Keycloak shows its form (it may redirect once inside its own pages).
  const page = count(http.get(authUrl, { tags: { name: "form" } }));
  const form = page.status === 200 ? page.html().find("form").first() : null;
  const action = form && form.attr("action");
  // The password goes only to the Keycloak of this run (the hosts are pinned to this machine anyway).
  if (!action || !action.startsWith(`${AUTH}/`)) return fail("form");

  // 3. The form goes back to Pharos's callback with a code.
  const submit = count(
    http.post(action, { username, password: PASSWORD, credentialId: "" }, { redirects: 0, tags: { name: "submit" } }),
  );
  const callbackUrl = submit.headers.Location;
  if (!redirects(submit) || !callbackUrl || !callbackUrl.startsWith(`${BASE}/api/auth/oidc/callback?`)) {
    return fail("submit");
  }

  // 4. The callback makes the session (code exchange, token checks, user creation).
  const callback = count(http.get(callbackUrl, { redirects: 0, tags: { name: "callback" } }));
  if (!redirects(callback) || !callback.cookies[COOKIE]) return fail("callback");

  // 5. The session is the person's own.
  const me = count(http.get(`${BASE}/api/me`, { tags: { name: "me" } }));
  if (me.status !== 200 || me.json("user.display_name") !== `Load User ${number}`) return fail("me");

  outcome.ok.add(1);
  signinMs.add(Date.now() - began);
  doneAtMs.add(Date.now() - exec.scenario.startTime);
}
