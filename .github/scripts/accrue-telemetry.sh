#!/usr/bin/env bash
# Append one smoke run's telemetry JSONL to the dedicated, long-lived `telemetry`
# branch, so data accrues durably across runs. main is branch-protected (can't be
# pushed to directly from CI); the `telemetry` branch is not, and keeping the data
# off main also keeps main's history clean.
#
# Inputs (env):
#   SRC          path to this run's telemetry JSONL (from the downloaded artifact)
#   BENCH_SRC    path to this run's hardware-benchmark JSONL (optional — a default
#                run with no benchmark sink produces none, and it is skipped)
#   RUN_ID       github.run_id (for the commit message); optional
# Requires: a checkout with push credentials (actions/checkout persist-credentials)
# and `contents: write` permission.
set -euo pipefail

SRC="${SRC:?SRC (path to run telemetry jsonl) required}"
BENCH_SRC="${BENCH_SRC:-}"
BRANCH="telemetry"
DEST="smoke-latency.jsonl"
BENCH_DEST="smoke-benchmark.jsonl"

# The latency stream is the primary signal; if it produced nothing there is
# nothing worth a commit even if a stray benchmark row exists.
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
  : > "$DEST"
fi

# Refresh the on-branch README every run (not only at orphan creation), so a
# pre-existing branch still documents a newly-added data file like
# smoke-benchmark.jsonl. Idempotent: unchanged content stages to nothing.
printf '# Smoke test telemetry (accrued)\n\n`smoke-latency.jsonl` — one JSON line per (provider, feature) per run.\n`smoke-benchmark.jsonl` — one hardware-quality grade per healthy lease per run (optional).\nBoth appended by `.github/workflows/provider-smoke.yml`.\nAnalyze latency with `uv run python -m just_akash.analyze_telemetry smoke-latency.jsonl`.\n' > README.md

cat "$SRC" >> "$DEST"
rows="$(wc -l < "$DEST" | tr -d ' ')"
git add "$DEST" README.md 2>/dev/null || git add "$DEST"

# Hardware grades ride into the same commit as a second file, so the quality
# analyzer can read them independently of the latency stream. Optional.
bench_new=0
if [ -s "$BENCH_SRC" ]; then
  cat "$BENCH_SRC" >> "$BENCH_DEST"
  bench_new="$(wc -l < "$BENCH_SRC" | tr -d ' ')"
  git add "$BENCH_DEST"
fi

git commit -m "telemetry: run ${RUN_ID:-local} (${rows} rows total)"
git push origin "HEAD:$BRANCH"
echo "Accrued $(wc -l < "$SRC" | tr -d ' ') new latency rows; ${rows} total on $BRANCH."
if [ "$bench_new" != "0" ]; then
  echo "Accrued ${bench_new} new benchmark rows; $(wc -l < "$BENCH_DEST" | tr -d ' ') total."
fi
