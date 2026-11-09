# Web image: Next.js standalone server. Build context: the repository root.
#   docker build -f frontend/Dockerfile .
FROM node:24-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS build

ENV NEXT_TELEMETRY_DISABLED=1
# pnpm is fetched at the version pinned in package.json ("packageManager").
RUN corepack enable

WORKDIR /app
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM node:24-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9

ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0

# The base image ships apt 2.6.1, which has a published HIGH vulnerability (CVE-2026-109518: an
# on-path attacker while apt downloads packages; fixed in 2.6.2). Nothing in this image runs apt
# after the build and the containers have no route to a package mirror, but the scanner is right
# to report it until the base image is rebuilt. Take the fixed apt now; remove this layer when
# node:24-slim ships apt 2.6.2 or later. Only the packages that carry the fix are upgraded.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends --only-upgrade apt libapt-pkg6.0 \
    && rm -rf /var/lib/apt/lists/*

# The running container needs only the node binary. npm and corepack, with the packages they bundle,
# are never used at run time, so remove them: less to patch, and nothing to misuse after a break-in.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
    /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /opt/yarn*

WORKDIR /app
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node

EXPOSE 3000
CMD ["node", "server.js"]
