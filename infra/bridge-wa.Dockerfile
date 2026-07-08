# Multi-stage build for the whatsmeow WhatsApp-App bridge (apps/bridge-wa).
# Pure-Go (CGO_ENABLED=0) thanks to modernc.org/sqlite, so the runtime is a
# tiny static binary on alpine. go.sum is generated at build time by `go mod
# tidy` (go.mod ships without pinned requires — see apps/bridge-wa/go.mod).
#
# Build context is the repo root (compose sets context: ..).
FROM golang:1.23-alpine AS build
RUN apk add --no-cache git ca-certificates
WORKDIR /src
COPY apps/bridge-wa/ ./
# GOTOOLCHAIN=auto lets `go` self-upgrade if a dep's go.mod requires a newer
# toolchain than the base image.
ENV CGO_ENABLED=0 GOOS=linux GOTOOLCHAIN=auto
RUN go mod tidy
RUN go build -trimpath -ldflags="-s -w" -o /out/bridge-wa .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget \
    && adduser -D -u 10001 bridge \
    && mkdir -p /data && chown -R bridge:bridge /data
COPY --from=build /out/bridge-wa /usr/local/bin/bridge-wa
USER bridge
# /data is chowned to the bridge user above so the mounted volume (anonymous or
# named bridge-wa-data) inherits that ownership on first creation.
VOLUME ["/data"]
EXPOSE 8100
ENTRYPOINT ["/usr/local/bin/bridge-wa"]
