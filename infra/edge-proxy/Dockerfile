# ai-tally edge proxy: self-hostable single binary (CTO-43).
#
# Multi-stage: build a fully static binary, then ship it on `scratch` so the runtime image is just
# the proxy (~10 MB) with no shell, package manager, or OS surface to attack; appropriate for
# running inside a regulated customer's VPC.

# --- build ------------------------------------------------------------------------------------
# Pinned to BUILDPLATFORM so the compiler always runs natively and only the OUTPUT is cross-targeted
# (CTO-334). The image is published for both linux/amd64 and linux/arm64: ECS Fargate runs it on
# ARM64 for the cost lever, while the Helm chart's audience is mostly x86 nodes. A stdlib-only Go
# binary cross-compiles for free, so a QEMU-emulated build stage would be pure waste.
FROM --platform=$BUILDPLATFORM golang:1.26 AS build
WORKDIR /src

# Set by buildx for every target in a multi-platform build; defaulted so a plain `docker build`
# (no buildx) still works and produces a binary for the host architecture.
ARG TARGETOS=linux
ARG TARGETARCH

# The module is stdlib-only (no go.sum / no external deps), so copying the source is enough.
COPY . .

# CGO off + static linking so the binary runs on scratch with no libc.
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -trimpath -ldflags="-s -w" \
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
