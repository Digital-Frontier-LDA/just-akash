#!/usr/bin/env bash
# Publish this run's bid-probe results onto the long-lived `telemetry` branch.
#
# Two files land there:
#   bidprobe-metrics.prom  — OVERWRITTEN each run. Every series is a gauge
#                            describing "right now", so history belongs in the
#                            JSONL, not here. Each cluster's Prometheus scrapes
#                            this raw URL directly.
#   bidprobe.jsonl         — APPENDED. The audit trail, and the raw material for
#                            measuring cron delivery/skew before anyone trusts
#                            the schedule.
#
# The branch must already exist. A remote flake that looks like "no such branch"
# must never cause us to create a fresh one: the .prom would briefly serve a
# world with no history behind it, and consumers cannot tell that apart from a
# fleet that genuinely stopped reporting.
set -euo pipefail

PROM_SRC="${PROM_SRC:-dl/bidprobe-metrics.prom}"
JSONL_SRC="${JSONL_SRC:-dl/bidprobe.jsonl}"
RUN_ID="${RUN_ID:-unknown}"

if [[ ! -s "$PROM_SRC" ]]; then
  echo "no bid-probe exposition at $PROM_SRC — nothing to publish"
  echo "(a crashed probe job produces no artifact; staleness rules will catch it)"
  exit 0
fi

# Hard-fail on a missing branch rather than letting the push create one.
if ! git ls-remote --exit-code --heads origin telemetry >/dev/null 2>&1; then
  echo "ERROR: origin/telemetry not found — refusing to create it" >&2
  exit 1
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git fetch --depth=1 origin telemetry
git checkout -B telemetry FETCH_HEAD

cp "$PROM_SRC" bidprobe-metrics.prom
if [[ -s "$JSONL_SRC" ]]; then
  cat "$JSONL_SRC" >> bidprobe.jsonl
fi

git add bidprobe-metrics.prom bidprobe.jsonl
if git diff --cached --quiet; then
  echo "nothing changed"
  exit 0
fi

git commit -m "chore(telemetry): bid-probe run ${RUN_ID}"
git push origin HEAD:telemetry
echo "published $(grep -c '^just_akash_bidprobe' bidprobe-metrics.prom) samples"
