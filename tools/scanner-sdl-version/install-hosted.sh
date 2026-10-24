#!/usr/bin/env bash
# Compile only this offline validator, before any hosted management credentials are used.
set -euo pipefail
[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted && "${RUNNER_OS:-}" == Linux ]]
[[ -n "${RUNNER_TEMP:-}" && "$RUNNER_TEMP" == /* && -d "$RUNNER_TEMP" ]]
[[ -n "${GITHUB_OUTPUT:-}" && "$GITHUB_OUTPUT" == /* ]]
[[ "$(env -i PATH="$PATH" GOENV=off GOTOOLCHAIN=local GOAUTH=off go version)" == 'go version go1.25.4 linux/amd64' ]]
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
work_dir="$(mktemp -d "$RUNNER_TEMP/scanner-official-version.XXXXXX")"
chmod 700 "$work_dir"
mkdir -m 700 "$work_dir/cache" "$work_dir/mod" "$work_dir/path" "$work_dir/tmp"
trap 'rm -rf -- "$work_dir/cache" "$work_dir/mod" "$work_dir/path" "$work_dir/tmp"' EXIT
(
  cd -- "$source_dir"
  timeout --signal=TERM --kill-after=10s 300s env -i PATH="$PATH" GOENV=off GOTOOLCHAIN=local GOWORK=off GOAUTH=off \
    GOPATH="$work_dir/path" GOMODCACHE="$work_dir/mod" GOCACHE="$work_dir/cache" \
    GOTMPDIR="$work_dir/tmp" TMPDIR="$work_dir/tmp" \
    GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org CGO_ENABLED=0 GOMAXPROCS=2 \
    go build -mod=readonly -trimpath -buildvcs=false -o "$work_dir/scanner-sdl-version" .
)
chmod 500 "$work_dir/scanner-sdl-version"
helper_sha="$(sha256sum "$work_dir/scanner-sdl-version")"
helper_sha="${helper_sha%% *}"
[[ "$helper_sha" =~ ^[0-9a-f]{64}$ ]]
printf 'helper-path=%s\nhelper-sha256=%s\n' "$work_dir/scanner-sdl-version" "$helper_sha" >> "$GITHUB_OUTPUT"
