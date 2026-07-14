#!/usr/bin/env bash
# Append one smoke run's telemetry JSONL to the dedicated, long-lived `telemetry`
# branch, so data accrues durably across runs. main is branch-protected (can't be
# pushed to directly from CI); the `telemetry` branch is not, and keeping the data
# off main also keeps main's history clean.
#
# Inputs (env):
#   SRC          path to this run's telemetry JSONL (from the downloaded artifact)
#   RUN_ID       github.run_id (for the commit message); optional
# Requires: a checkout with push credentials (actions/checkout persist-credentials)
# and `contents: write` permission.
set -euo pipefail

SRC="${SRC:?SRC (path to run telemetry jsonl) required}"
BRANCH="telemetry"
DEST="smoke-latency.jsonl"

if [ ! -s "$SRC" ]; then
  echo "No telemetry to accrue (missing/empty $SRC) — nothing to do."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Switch to the telemetry branch, creating it as a clean orphan the first time.
if git fetch origin "$BRANCH" 2>/dev/null && git checkout -B "$BRANCH" "origin/$BRANCH"; then
  echo "Checked out existing $BRANCH."
else
  echo "Creating orphan $BRANCH."
  git checkout --orphan "$BRANCH"
  git rm -rf . >/dev/null 2>&1 || true
  printf '# Smoke test latency telemetry (accrued)\n\nOne JSON line per (provider, feature) per run, appended by `.github/workflows/provider-smoke.yml`.\nAnalyze with `uv run python -m just_akash.analyze_telemetry smoke-latency.jsonl`.\n' > README.md
  : > "$DEST"
fi

cat "$SRC" >> "$DEST"
rows="$(wc -l < "$DEST" | tr -d ' ')"
git add "$DEST" README.md 2>/dev/null || git add "$DEST"
git commit -m "telemetry: run ${RUN_ID:-local} (${rows} rows total)"
git push origin "HEAD:$BRANCH"
echo "Accrued $(wc -l < "$SRC" | tr -d ' ') new rows; ${rows} total on $BRANCH."
