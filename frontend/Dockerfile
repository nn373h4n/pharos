# Web image: Next.js standalone server. Build context: the repository root.
#   docker build -f frontend/Dockerfile .
FROM node:24-slim AS build

ENV NEXT_TELEMETRY_DISABLED=1
# pnpm is fetched at the version pinned in package.json ("packageManager").
RUN corepack enable

WORKDIR /app
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM node:24-slim

ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node

EXPOSE 3000
CMD ["node", "server.js"]
