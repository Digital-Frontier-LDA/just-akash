#!/usr/bin/env bash
# Publish the canary's durable state + exposition onto the long-lived `telemetry` branch,
# alongside the smoke data df-grafana already scrapes. main is branch-protected; the
# telemetry branch is data-only and not.
#
# Three files live there for the canary, and the split matters:
#   canary-targets.json   provider -> {uri, dseq}. Written by canary/ensure.py. This is
#                         how the next run knows where to look and which lease it is
#                         looking at — the dseq is what separates "restarted" from
#                         "replaced".
#   canary-state.json     durable cumulative counters. A restart wipes the agent, so the
#                         counting cannot live in the agent; it lives here.
#   canary-metrics.prom   what df-grafana scrapes.
#
# Inputs (env): SRC_PROM, SRC_STATE, SRC_TARGETS, RUN_ID (optional)
# Requires a checkout with push credentials and `contents: write`.
set -euo pipefail

SRC_PROM="${SRC_PROM:?SRC_PROM required}"
SRC_STATE="${SRC_STATE:?SRC_STATE required}"
SRC_TARGETS="${SRC_TARGETS:?SRC_TARGETS required}"
BRANCH="telemetry"

# An empty exposition would blank every canary series in df-grafana at once, which reads
# as "all three providers vanished" — indistinguishable from a real fleet-wide outage.
# Refuse to publish nothing.
if [ ! -s "$SRC_PROM" ]; then
  echo "Refusing to publish an empty exposition ($SRC_PROM) — leaving the last good one."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Distinguish "branch missing" (legitimate first run) from "remote unreachable" (must be a
# hard failure). Publishing a first-run file over an unreachable remote is impossible
# anyway, and treating the two the same is how a transient network blip would reset every
# cumulative counter to zero — firing increase()-based alerts fleet-wide.
if ! ls_out=$(git ls-remote --heads origin "$BRANCH" 2>&1); then
  echo "Cannot reach origin to check for '$BRANCH': $ls_out" >&2
  exit 1
fi

tmp=$(mktemp -d)
if [ -n "$ls_out" ]; then
  git fetch --depth=1 origin "$BRANCH"
  git worktree add --detach "$tmp" FETCH_HEAD
  git -C "$tmp" checkout -B "$BRANCH"
else
  echo "Branch '$BRANCH' does not exist yet — creating it."
  git worktree add --detach "$tmp"
  git -C "$tmp" checkout --orphan "$BRANCH"
  git -C "$tmp" rm -rf . >/dev/null 2>&1 || true
fi

cp "$SRC_PROM"    "$tmp/canary-metrics.prom"
cp "$SRC_STATE"   "$tmp/canary-state.json"
cp "$SRC_TARGETS" "$tmp/canary-targets.json"

git -C "$tmp" add canary-metrics.prom canary-state.json canary-targets.json
if git -C "$tmp" diff --cached --quiet; then
  echo "No canary change to publish."
else
  git -C "$tmp" commit -m "data(canary): collection from run ${RUN_ID:-manual}"
  git -C "$tmp" push origin "$BRANCH"
  echo "Published canary telemetry to '$BRANCH'."
fi
git worktree remove --force "$tmp"
