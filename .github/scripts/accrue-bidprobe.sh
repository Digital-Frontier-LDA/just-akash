#!/usr/bin/env bash
# Publish this run's bid-probe results onto the long-lived `telemetry` branch.
#
# Three files land there:
#   bidprobe-metrics.prom  — OVERWRITTEN each run. Every series is a gauge
#                            describing "right now", so history belongs in the
#                            JSONL, not here. Each cluster's Prometheus scrapes
#                            this raw URL directly.
#   bidprobe.jsonl         — APPENDED. Per-pair audit trail.
#   bidprobe-runs.jsonl    — APPENDED, one line per RUN: when the cron was due
#                            versus when the run actually started. GitHub's cron
#                            is best-effort and this repo has measured 24-211
#                            minutes of skew, so "is a 3h schedule good enough to
#                            be the only bid-health trigger" must be answered
#                            from data. The Actions API ages runs out; this
#                            branch does not.
#
# The run line is written even when the probe produced NO exposition. A run that
# fired and failed is still a delivered run, and excluding those would flatter
# the delivery rate — which is the one number Stage 4 exists to measure.
#
# The branch must already exist. A remote flake that looks like "no such branch"
# must never cause us to create a fresh one: the .prom would briefly serve a
# world with no history behind it, and consumers cannot tell that apart from a
# fleet that genuinely stopped reporting.
set -euo pipefail

PROM_SRC="${PROM_SRC:-dl/bidprobe-metrics.prom}"
JSONL_SRC="${JSONL_SRC:-dl/bidprobe.jsonl}"
RUN_ID="${RUN_ID:-unknown}"

# When the run actually began, captured by the runner's first step. NOT from
# `github.run_started_at` — that property does not exist in the github context,
# and referencing it yields an empty string, which would silently record no runs
# at all while every step still reported success.
RUN_STARTED_AT=""
if [[ -s "${RUN_STARTED_FILE:-}" ]]; then
  RUN_STARTED_AT="$(head -n1 "${RUN_STARTED_FILE}" | tr -d '[:space:]')"
fi
if [[ -z "$RUN_STARTED_AT" ]]; then
  echo "WARN: no run-start marker at ${RUN_STARTED_FILE:-<unset>} — delivery/skew cannot be measured for this run" >&2
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

if [[ -n "${RUN_STARTED_AT:-}" ]]; then
  printf '{"run_id":"%s","event":"%s","schedule":"%s","started_at":"%s","attempt":"%s","accrued_at":"%s","published":%s}\n' \
    "${RUN_ID}" "${RUN_EVENT:-}" "${RUN_SCHEDULE:-}" "${RUN_STARTED_AT}" \
    "${RUN_ATTEMPT:-1}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$([[ -s "$PROM_SRC" ]] && echo true || echo false)" \
    >> bidprobe-runs.jsonl
  git add bidprobe-runs.jsonl
fi

if [[ -s "$PROM_SRC" ]]; then
  cp "$PROM_SRC" bidprobe-metrics.prom
  git add bidprobe-metrics.prom
  if [[ -s "$JSONL_SRC" ]]; then
    cat "$JSONL_SRC" >> bidprobe.jsonl
    git add bidprobe.jsonl
  fi
else
  # A scoped (--cluster) run deliberately publishes no exposition, and a crashed
  # probe job produces no artifact at all. Both are recorded above as a run that
  # happened; the staleness rule is what notices if it keeps happening.
  echo "no exposition at $PROM_SRC — recording the run, publishing nothing"
fi

if git diff --cached --quiet; then
  echo "nothing changed"
  exit 0
fi

git commit -m "chore(telemetry): bid-probe run ${RUN_ID}"
git push origin HEAD:telemetry

if [[ -s bidprobe-metrics.prom ]]; then
  echo "published $(grep -c '^just_akash_bidprobe' bidprobe-metrics.prom) samples"
fi
