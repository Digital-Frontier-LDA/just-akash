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
#   CREDIT_SRC   path to this run's deploy-credit snapshot (`balance --check --json`
#                output; optional — rendered into the credit gauge when present)
#   RUN_ID       github.run_id (for the commit message); optional
# Requires: a checkout with push credentials (actions/checkout persist-credentials)
# and `contents: write` permission.
set -euo pipefail

SRC="${SRC:?SRC (path to run telemetry jsonl) required}"
BENCH_SRC="${BENCH_SRC:-}"
CREDIT_SRC="${CREDIT_SRC:-}"
BRANCH="telemetry"
DEST="smoke-latency.jsonl"
BENCH_DEST="smoke-benchmark.jsonl"
PROM_DEST="smoke-metrics.prom"

# The latency stream is the primary signal; if it produced nothing there is
# nothing worth a commit even if a stray benchmark row exists.
if [ ! -s "$SRC" ]; then
  echo "No telemetry to accrue (missing/empty $SRC) — nothing to do."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# ── Render the Prometheus textfile snapshot BEFORE switching branches ─────────
# The .prom must be rendered from the FULL accrued dataset (existing branch data +
# this run), and the exporter lives in the just_akash package — which vanishes from
# the working tree the moment we `git checkout -B telemetry`. So: reconstruct the
# post-append dataset in a temp dir now, render there (pure stdlib — the runner's
# python3 needs no dependency install), and copy the result in after the switch.
# Best-effort: a render failure warns but never sinks the data accrual itself.
RENDER_TMP="$(mktemp -d)"
trap 'rm -rf "$RENDER_TMP"' EXIT
PROM_RENDERED=""
{
  if git fetch origin "$BRANCH" 2>/dev/null; then
    git show "FETCH_HEAD:$DEST" > "$RENDER_TMP/latency.jsonl" 2>/dev/null || : > "$RENDER_TMP/latency.jsonl"
    git show "FETCH_HEAD:$BENCH_DEST" > "$RENDER_TMP/bench.jsonl" 2>/dev/null || : > "$RENDER_TMP/bench.jsonl"
  else
    : > "$RENDER_TMP/latency.jsonl"
    : > "$RENDER_TMP/bench.jsonl"
  fi
  cat "$SRC" >> "$RENDER_TMP/latency.jsonl"
  if [ -s "$BENCH_SRC" ]; then cat "$BENCH_SRC" >> "$RENDER_TMP/bench.jsonl"; fi

  render_args=()
  if [ -s "$RENDER_TMP/bench.jsonl" ]; then render_args+=(--benchmark "$RENDER_TMP/bench.jsonl"); fi
  if [ -n "$CREDIT_SRC" ] && [ -s "$CREDIT_SRC" ]; then render_args+=(--credit-json "$CREDIT_SRC"); fi
  if python3 -m just_akash.prometheus_exporter "$RENDER_TMP/latency.jsonl" \
       "${render_args[@]}" --output "$RENDER_TMP/$PROM_DEST"; then
    PROM_RENDERED="$RENDER_TMP/$PROM_DEST"
    echo "Rendered $(grep -c '^just_akash' "$PROM_RENDERED" || true) metric sample(s)."
  else
    echo "WARNING: metrics render failed — accruing data without a .prom update." >&2
  fi
} || echo "WARNING: metrics render errored — accruing data without a .prom update." >&2

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
printf '# Smoke test telemetry (accrued)\n\n`smoke-latency.jsonl` — one JSON line per (provider, feature) per run.\n`smoke-benchmark.jsonl` — one hardware-quality grade per healthy lease per run (optional).\n`smoke-metrics.prom` — the SAME data rendered as Prometheus textfile-collector metrics,\nre-rendered every run (fetch it raw and serve it to a Prometheus scrape).\nAll written by `.github/workflows/provider-smoke.yml`.\nAnalyze latency with `uv run python -m just_akash.analyze_telemetry smoke-latency.jsonl`.\n' > README.md

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

# The rendered Prometheus snapshot rides in the same commit, so one raw-URL fetch
# of this branch always sees data + metrics in lockstep.
if [ -n "$PROM_RENDERED" ] && [ -s "$PROM_RENDERED" ]; then
  cp "$PROM_RENDERED" "$PROM_DEST"
  git add "$PROM_DEST"
fi

git commit -m "telemetry: run ${RUN_ID:-local} (${rows} rows total)"
git push origin "HEAD:$BRANCH"
echo "Accrued $(wc -l < "$SRC" | tr -d ' ') new latency rows; ${rows} total on $BRANCH."
if [ "$bench_new" != "0" ]; then
  echo "Accrued ${bench_new} new benchmark rows; $(wc -l < "$BENCH_DEST" | tr -d ' ') total."
fi
