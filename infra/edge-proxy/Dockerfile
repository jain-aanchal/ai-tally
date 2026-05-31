# ai-tally edge proxy — self-hostable single binary (CTO-43).
#
# Multi-stage: build a fully static binary, then ship it on `scratch` so the runtime image is just
# the proxy (~10 MB) with no shell, package manager, or OS surface to attack — appropriate for
# running inside a regulated customer's VPC.

# --- build ------------------------------------------------------------------------------------
FROM golang:1.26 AS build
WORKDIR /src

# The module is stdlib-only (no go.sum / no external deps), so copying the source is enough.
COPY . .

# CGO off + static linking so the binary runs on scratch with no libc.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" \
    -o /out/edge-proxy ./cmd/edge-proxy

# --- runtime ----------------------------------------------------------------------------------
FROM scratch
# CA roots so the proxy can dial the upstream provider over TLS.
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/edge-proxy /edge-proxy

EXPOSE 8088
# Runs as an unprivileged uid; scratch has no /etc/passwd so we use a numeric id.
USER 65532:65532
ENTRYPOINT ["/edge-proxy"]
