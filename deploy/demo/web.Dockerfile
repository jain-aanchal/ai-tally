# syntax=docker/dockerfile:1
# ai-tally dashboard (Next.js 15 / React 19) - demo-deploy-kit production image (CTO-243).
#
# Unlike web/Dockerfile (whose build context is web/), this image builds from the REPO ROOT so the
# whole kit can be built with one command from the checkout:
#
#   docker build -f deploy/demo/web.Dockerfile -t ai-tally-web-demo .
#
# It is functionally the same standalone build as web/Dockerfile, just re-pathed for a root context.
#
# Multi-stage:
#   deps    - install node_modules from the lockfile (cache-friendly layer).
#   builder - `next build` in standalone mode, emitting a self-contained server.
#   runner  - a slim runtime carrying only the standalone server + static assets.
#
# Standalone output: web/next.config.mjs is intentionally NOT modified by this ticket (additive
# only), so rather than setting `output: "standalone"` in the config we force it at build time with
# NEXT_PRIVATE_STANDALONE=true. Next.js reads that env and emits `.next/standalone` exactly as the
# config flag would.

# ---- deps ---------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS deps
WORKDIR /app
# Only the manifests, so this layer is reused whenever source (but not deps) changes.
COPY web/package.json web/package-lock.json ./
RUN npm ci

# ---- builder ------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PRIVATE_STANDALONE=true
COPY --from=deps /app/node_modules ./node_modules
COPY web/ .
RUN npm run build

# ---- runner -------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    # Next's standalone server honours PORT/HOSTNAME. 0.0.0.0 so the container is reachable from the
    # other compose services (Caddy proxies to web:3000).
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Run as an unprivileged, numeric non-root user.
RUN groupadd --system --gid 1001 nodejs \
 && useradd  --system --uid 1001 --gid nodejs nextjs

# The standalone build bundles a minimal node_modules and a server.js. Static assets are served
# from .next/static and must be copied alongside it (standalone does not inline them).
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
