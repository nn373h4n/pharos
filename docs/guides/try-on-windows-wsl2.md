# Trying Pharos on Windows 11 (WSL2)

For a look at Pharos on a Windows 11 computer: a demo, a test of the screens, a first sign-in.
**Not for a pilot or for real data.** Pharos is supported on Linux only
([DEPLOYMENT.md, "Requirements"](../DEPLOYMENT.md#requirements)).

**This guide has not been followed on Windows by the maintainers.** Every Pharos command in it was
run on Linux on the published v0.1.0 images (the M5-11 rehearsal); the Windows and WSL steps come
from their vendors' documentation. If a step does not work, open an issue with the step and the
message.

## What does not work on WSL2, and why it is fine for a test

| Not available | Why | What to do in a test |
| --- | --- | --- |
| Egress rules (`deploy/egress/pharos-egress.sh`, gate G7) | Docker Desktop runs containers in its own hidden machine; the rules never reach it, and WSL's kernel was never tested with them | Do not run the script; keep `PHAROS_PERSONAL_FEEDS=off` |
| Running all the time | WSL stops when it is idle, when you sign out or when Windows restarts | Start the stack again when you need it |
| Scheduled backups and retention | Same reason; no cron you can rely on | Not needed for a test |
| A real certificate | The site is `localhost` | `PHAROS_TLS=internal` and accept the browser warning (below) |

## 1. WSL2 and Ubuntu

In PowerShell **as administrator**:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart Windows when asked, open "Ubuntu 24.04" from the Start menu and create your Linux user.

Give WSL enough memory: create `%UserProfile%\.wslconfig` with

```ini
[wsl2]
memory=8GB
processors=4
```

then run `wsl --shutdown` in PowerShell and open Ubuntu again. (The checks ran on 2 CPUs and 8 GB;
with less memory the stack may still start, but slowly.) Leave WSL's network mode at its default
(NAT): then the site is reachable from this computer only, not from the rest of your network.

## 2. Docker

Choose one:

- **Docker Engine inside Ubuntu (recommended for a test; free).** In the Ubuntu terminal, follow
  Docker's "Install Docker Engine on Ubuntu" page (the `apt` repository method), then
  `sudo usermod -aG docker $USER`, close the terminal and open it again.
- **Docker Desktop for Windows**, with "Use the WSL 2 based engine" and the integration for
  Ubuntu-24.04 switched on. Docker Desktop needs a paid subscription in larger organizations (more
  than 250 employees or more than USD 10 million a year, by Docker's terms at the time of writing).

Do not install both. Check, in the Ubuntu terminal:

```bash
docker version --format '{{.Server.Version}}'   # 26 or newer
docker compose version                          # v2.24.4 or newer
sudo apt-get update && sudo apt-get install -y git make openssl
```

## 3. Install a release

Everything from here runs in the Ubuntu terminal, in your Linux home directory. **Do not clone into
`/mnt/c/...`** (a Windows folder): file modes there are not kept, and the scripts refuse files and
folders that others can read.

Follow step 1 of the [pilot runbook](pilot-runbook.md#1-install) as written: clone, check out the
tag, `make env`, put the two digests from the release notes into `deploy/.env` and run
`scripts/verify-release.sh`. Then set these values in `deploy/.env` (`nano deploy/.env`):

```text
PHAROS_DOMAIN=localhost
PHAROS_TLS=internal
PHAROS_HTTP_PORT=8080
PHAROS_HTTPS_PORT=8443
PHAROS_PUBLIC_URL=https://localhost:8443
PHAROS_HSTS_MAX_AGE=0
PHAROS_PERSONAL_FEEDS=off
```

Ports 8080 and 8443 avoid a clash with anything on Windows that holds 80 or 443. `PHAROS_HSTS_MAX_AGE=0`
keeps your browser from remembering "https only" for every `localhost` port. Leave
`PHAROS_ENVIRONMENT=production`, `PHAROS_LLM_PROVIDER=off` and `PHAROS_EMBEDDING_PROVIDER=off`.

Continue with the runbook: define `pharos`, check `pharos config --images`, `pharos up -d`, create
the organization and the administrator, and run `scripts/status.sh`. Every line should say PASS
(the backup line says NOT CHECKED). The first `up -d` downloads about 1.2 GB of images.

## 4. Open it in Windows

Open `https://localhost:8443` in a browser on Windows. WSL passes `localhost` through to Ubuntu.

The browser warns that the certificate is not trusted: the site uses Caddy's own test authority.
For a test, continue past the warning ("Advanced", then "Continue to localhost"). Do not install
that authority into Windows' trusted roots: whoever has its key could then impersonate any site
to this computer.

Sign in with the password form and the administrator you created. To see articles, import the
starter sources (step 3 of the runbook) and switch one or two on in the Sources page; the first
fetch takes a minute or two.

Single sign-on and the model are optional in a test. For single sign-on, register
`https://localhost:8443/api/auth/oidc/callback` at the provider ([SSO guides](sso-entra.md)); use a
test tenant or a test application, not the one your organization will use for the pilot.

## 5. Stop and remove

```bash
pharos stop        # stop; data is kept
pharos up -d       # start again
pharos down -v     # remove the containers and every volume of this installation, with all its data
```

To remove WSL's Ubuntu entirely: `wsl --unregister Ubuntu-24.04` in PowerShell (this deletes
everything inside it).
