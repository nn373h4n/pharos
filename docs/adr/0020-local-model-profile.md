# 0020. The local model profile (Ollama)

- Status: Proposed
- Date: 2026-11-02

## Context

The plan for M4 left "fully local" open (question 11, decision D7) and asked for a `local-llm`
Compose profile with Ollama once the cloud path worked. Some installations cannot send even public
article text to a provider, and the cost per token of a small local model is zero. Two things make
it a security decision, not just a convenience:

- the worker would call a service inside the network, while every other outbound call of the
  model layer goes to a provider's public HTTPS name, which is looked up first (ADR-0015, M4-03);
- a model server needs model files, which come from the internet, and a model file is a large
  binary that the server loads and runs.

Nothing here claims that a local model is good enough. That is the Azerbaijani evaluation (M4-09)
and it is not done (F-36).

## Options considered

1. **Let an administrator enter any base URL for the local provider.** Pros: works with any
   server. Cons: the worker, which fetches hostile input, could be pointed at any address inside
   the network, which is the very thing the address rules exist to stop.
2. **Run the model server on the same network as everything else.** Pros: simplest. Cons: a
   compromised or buggy model server would have a route to the internet and to the database.
3. **Reach it by name, `http://ollama:11434`.** Pros: no address to configure. Cons: found in the
   review: a name that does not resolve (the server is down or restarting) falls through to the
   host's resolver, so a search domain or a wildcard could answer, and an `HTTP_PROXY` of the
   environment would carry every prompt to the proxy. Plain HTTP makes both worse.
4. **A fixed address on an internal network of its own, models loaded by a separate one-shot job
   (chosen).** Pros: the server has no route of its own out; only the worker shares its network;
   nothing can resolve elsewhere; downloading is an explicit operator action. Cons: two steps for
   the operator, and a range that may clash with the host's networks.

## Decision

Option 4.

- Provider `ollama`. It takes **no URL and no key**: the settings refuse `PHAROS_LLM_BASE_URL` and
  `PHAROS_LLM_API_KEY` with it. The server is at one **IP address**, `PHAROS_OLLAMA_IP`
  (default `172.28.30.10`), on the subnet `PHAROS_OLLAMA_SUBNET`; the Compose file pins the server
  to it. The setting accepts only a private IPv4 address (no name, no loopback, no link-local, no
  public address), so an administrator picks a provider, never a place on the network. The call
  uses Ollama's OpenAI-compatible API and the same client as the other providers: no redirect is
  followed, no tool is sent, `num_retries=0`. For this provider the client ignores the
  environment's proxy settings (`trust_env=False`).
- The public-name lookup that remote endpoints get is skipped for this one address, because it is
  inside the network on purpose. A test keeps the lookup for every remote endpoint.
- Compose: service `ollama` in the profile `local-llm`, on the internal network `llm` only (no
  gateway to the internet, no published port, not on `data`, `app` or `egress`); the worker is the
  only other member of `llm`. The image is pinned by tag and digest (16 days old when added) and
  runs as a non-root user with the usual hardening. It mounts the models **read-only**, loads one
  model and serves one request at a time, and has memory and processor limits. Nothing waits for
  it, so the default stack is unchanged.
- A new volume belongs to root, so a helper, `ollama-init`, runs for a moment with no network and
  one capability (`CHOWN`) and gives the volume to the user the model services run as.
- Models are loaded by a second service, `ollama-pull`, in the profile `local-llm-pull`, run by an
  operator with `scripts/ollama-pull.sh <model>`. The name is checked against a strict pattern and
  a name that points at another registry is refused. The job has a network of its own (`pull`), so
  while it has a route to the internet it shares nothing with the API or the worker.
- Cost: a local model costs nothing per token, so its prices are zero and the daily money cap does
  not stop it; the calls-per-minute limit, the timeouts and the server's limits still do.
- What is **not** claimed:
  - "No route out" means the network has no gateway to the internet. As for the other internal
    networks, a service on the host that listens on all interfaces can be reached through the
    network's gateway address; if such a service is a proxy, a compromised server could use it.
    The host firewall rule for the Docker networks (F-06) should name the `llm` range too.
  - "Fully local": the worker keeps its `egress` network because it fetches sources, so removing
    every outbound route is a firewall or proxy decision (F-04).
  - Quality: nothing is said about how good a local model is until the evaluation has run on it.

## Consequences

- Positive: the local option exists with a small, testable surface; the model server cannot send
  anything out by itself and cannot change its models; the same evaluation harness can measure it
  (`PHAROS_LLM_PROVIDER=ollama`); `scripts/local-llm-check.sh` repeats the isolation checks on a
  real server.
- Negative: model files are not checked beyond the name an operator gives; a downloaded model is
  trusted as the registry serves it. An address range must be chosen that does not clash with the
  host. The memory a model needs is the operator's to size (`PHAROS_OLLAMA_MEMORY`). GPU use is not
  set up. The live check is not part of the pipeline (the image is several GB).
- Follow-up: pin a model by digest when Ollama offers a way that fits this flow; run the
  Azerbaijani evaluation on a local model (F-36) before anyone calls the mode usable.

## References

- ADR-0002 (gateway), ADR-0010 (network topology), ADR-0015 (what may reach a model), ADR-0016
  (budget); `docs/plans/m4-llm-layer-and-personal-feed.md` (D7); `docs/DEPLOYMENT.md`
