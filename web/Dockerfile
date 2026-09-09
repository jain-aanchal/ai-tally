# ai-tally dashboard (Next.js 15 / React 19): production image.
#
# Build context is the `web/` directory:
#   docker build -t REGION-docker.pkg.dev/PROJECT/ai-tally/web:TAG web/
#
# Multi-stage:
#   deps:     install node_modules from the lockfile (cache-friendly layer).
#   builder:  `next build` in standalone mode, emitting a self-contained server.
#   runner:   a slim runtime carrying only the standalone server + static assets.
#
# Standalone output: the repo's web/next.config.mjs is intentionally NOT modified by this ticket
# (additive-only), so instead of setting `output: "standalone"` in the config we force it at build
# time with NEXT_PRIVATE_STANDALONE=true. Next.js reads that env and emits `.next/standalone`
# exactly as the config flag would. If the web team later adds `output: "standalone"` to
# next.config.mjs, this env becomes a redundant no-op; leave it or drop it, either works.

# ---- deps ---------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS deps
WORKDIR /app
# Only the manifests, so this layer is reused whenever source (but not deps) changes.
#
# .npmrc belongs here too (CTO-334). It carries legacy-peer-deps=true, which the repo needs because
# @clerk/nextjs pins its React peer above the exact react 19.0.0 the app is built against. Without it
# `npm ci` dies with ERESOLVE and the image cannot be built at all. That went unnoticed until CI
# started building this image, because the `web` CI job runs npm ci in the checkout, where .npmrc is
# simply present.
COPY package.json package-lock.json .npmrc ./
RUN npm ci

# ---- builder ------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PRIVATE_STANDALONE=true
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runner -------------------------------------------------------------------------------------
FROM node:22-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    # Next's standalone server honours PORT/HOSTNAME. Cloud Run overrides PORT (8080); GKE sets it
    # via the chart. 0.0.0.0 so the container is reachable from outside the pod/instance.
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Run as an unprivileged, numeric non-root user (matches the edge-proxy chart's posture).
RUN groupadd --system --gid 1001 nodejs \
 && useradd  --system --uid 1001 --gid nodejs nextjs

# The standalone build bundles a minimal node_modules and a server.js. Static assets are served
# from .next/static and must be copied alongside it (standalone does not inline them).
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
