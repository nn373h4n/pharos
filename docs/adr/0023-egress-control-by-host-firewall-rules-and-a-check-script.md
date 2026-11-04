# 0023. Egress control by host firewall rules and a check script

- Status: Proposed
- Date: 2026-11-04

## Context

Principle 5 of the proposal is default-deny egress. ADR-0010 puts `postgres`, `redis` and `web` on
`internal: true` networks, so Docker itself stops them from calling out. It cannot restrict
*which* internet hosts the services on the `egress` network (`api`, `worker`, `beat`) may reach, or
the host addresses that Docker's bridge gateway exposes to containers. This is the main gap
against the stated principle (F-04, F-06) and is listed in the threat model.

Two facts shape what a rule can do. The API needs only a few destinations: the identity provider
and Microsoft Graph. The worker is different: administrators and every person (personal feeds)
may point it at any public host, and the model gateway's HTTPS calls go through the model client,
not `SafeHttpClient`. So a host rule **cannot** allow-list the worker's feed hosts by name.

## Options considered

1. **Host firewall rules and a check script** — pros: no new service; the rules are written so the
   pilot organization's IT can apply and rerun them. Cons: the worker can still reach any public
   address on the allowed ports; the rules live on the host, outside Docker.
2. **An outbound proxy profile** (a new service) — pros: allow-listing by host name is possible
   where a proxy can see it. Cons: a new always-running service and a new thing to attack; the
   self-hosting rule is no new service without a measured need.
3. **Leave it to the operator** — pros: nothing to build. Cons: the principle stays a sentence in
   a document.

## Decision

Option 1. A rule set, and `scripts/egress-check.sh` that proves each rule with a positive control
(a call that must be refused and one that must pass).

- `api`: the identity provider and Graph on 443 only.
- `worker`: public addresses on 80 and 443 only; no private ranges, no cloud metadata address, no
  Docker bridge gateway.
- `beat`: no database address needed (F-21 is part of the task).
- The rules are proven in a disposable VM or a network namespace, **never by changing the shared
  development host's firewall**. If no disposable host exists, the check is NOT RUN and is carried
  to gate G7 on the pilot host.
- An outbound proxy is considered again only if the pilot organization's IT requires one.

This amends the part of ADR-0010 that leaves destination control open; the network topology of
ADR-0010 stays.

## Consequences

- Positive: the gap in the threat model becomes a rule set that can be checked and repeated; no new
  service.
- Negative: a compromised worker can still call any public host on 80 and 443 (feed hosts cannot be
  named in advance); that residual risk stays written in the threat model. Rules differ per host
  firewall (nftables, ufw, cloud security groups).
- Follow-up: M5-03 builds the rules and the script; the threat model's residual list is updated;
  gate G7 is the check on the pilot host, passed by the maintainer or the pilot organization's IT.

## References

- F-04, F-06, F-21 in `TASKS.md`; ADR-0010; `docs/THREAT_MODEL.md` residual risks;
  `docs/plans/m5-pilot-readiness.md` (M5-03, D2, gate G7).
