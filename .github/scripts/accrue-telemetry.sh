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
#                output; optional — rendered into the credit gauge when present,
#                and persisted on the branch so a failed snapshot falls back to
#                the last known value instead of dropping the gauge)
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
CREDIT_DEST="deploy-credit.json"

# The latency stream is the primary signal; if it produced nothing there is
# nothing worth a commit even if a stray benchmark row exists.
if [ ! -s "$SRC" ]; then
  echo "No telemetry to accrue (missing/empty $SRC) — nothing to do."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# ── Establish branch state ONCE, distinguishing the three cases ───────────────
# "branch exists" / "branch doesn't exist yet" (a clean first run) / "can't reach
# the remote". The last one must be a HARD failure: treating it as branch-missing
# would render the .prom from this run's ~30 rows instead of the full accrued
# dataset — Prometheus would see every counter collapse and then jump back the
# next day, firing increase()-based alerts fleet-wide. (And an unreachable remote
# means the push below could not succeed anyway.)
if ! branches=$(git ls-remote --heads origin "$BRANCH" 2>&1); then
  echo "ERROR: cannot reach remote to check for the $BRANCH branch:" >&2
  echo "$branches" >&2
  exit 1
fi
BRANCH_EXISTS=0
if echo "$branches" | grep -q "refs/heads/$BRANCH"; then
  BRANCH_EXISTS=1
  # The fetch must succeed — after a positive ls-remote, a failure here is a
  # remote flake, not a missing branch; falling through to the orphan path would
  # build an unrelated history whose push gets rejected and lose this run's rows.
  git fetch origin "$BRANCH"
fi

# ── Render the Prometheus textfile snapshot BEFORE switching branches ─────────
# The .prom must be rendered from the FULL accrued dataset (existing branch data +
# this run), and the exporter lives in the just_akash package — which vanishes from
# the working tree the moment we switch to the telemetry branch. So: reconstruct
# the post-append dataset in a temp dir now, render there (pure stdlib — the
# runner's python3 needs no dependency install), and copy the result in after the
# switch. A render failure warns but never sinks the data accrual itself.
RENDER_TMP="$(mktemp -d)"
trap 'rm -rf "$RENDER_TMP"' EXIT
PROM_RENDERED=""
{
  : > "$RENDER_TMP/latency.jsonl"
  : > "$RENDER_TMP/bench.jsonl"
  if [ "$BRANCH_EXISTS" = "1" ]; then
    # A file can legitimately be absent on an existing branch (e.g. the benchmark
    # stream predates it) — that alone must not abort the render.
    git show "FETCH_HEAD:$DEST" > "$RENDER_TMP/latency.jsonl" 2>/dev/null || : > "$RENDER_TMP/latency.jsonl"
    git show "FETCH_HEAD:$BENCH_DEST" > "$RENDER_TMP/bench.jsonl" 2>/dev/null || : > "$RENDER_TMP/bench.jsonl"
  fi
  cat "$SRC" >> "$RENDER_TMP/latency.jsonl"
  if [ -s "$BENCH_SRC" ]; then cat "$BENCH_SRC" >> "$RENDER_TMP/bench.jsonl"; fi

  # Credit gauge input: this run's snapshot, falling back to the last snapshot
  # persisted on the branch — so one failed `balance` call (the snapshot step is
  # best-effort) doesn't make just_akash_deploy_credit_usd vanish from the fleet's
  # dashboards and silently disarm the credit-low alert (which is NoData=OK).
  CREDIT_FOR_RENDER=""
  if [ -n "$CREDIT_SRC" ] && [ -s "$CREDIT_SRC" ]; then
    CREDIT_FOR_RENDER="$CREDIT_SRC"
  elif [ "$BRANCH_EXISTS" = "1" ] && git show "FETCH_HEAD:$CREDIT_DEST" > "$RENDER_TMP/credit.json" 2>/dev/null; then
    CREDIT_FOR_RENDER="$RENDER_TMP/credit.json"
    echo "No fresh deploy-credit snapshot; rendering the gauge from the branch's last known value."
  fi

  render_args=()
  if [ -s "$RENDER_TMP/bench.jsonl" ]; then render_args+=(--benchmark "$RENDER_TMP/bench.jsonl"); fi
  if [ -n "$CREDIT_FOR_RENDER" ]; then render_args+=(--credit-json "$CREDIT_FOR_RENDER"); fi
  if python3 -m just_akash.prometheus_exporter "$RENDER_TMP/latency.jsonl" \
       "${render_args[@]}" --output "$RENDER_TMP/$PROM_DEST"; then
    PROM_RENDERED="$RENDER_TMP/$PROM_DEST"
    echo "Rendered $(grep -c '^just_akash' "$PROM_RENDERED" || true) metric sample(s)."
  else
    echo "WARNING: metrics render failed — accruing data without a .prom update." >&2
  fi
} || echo "WARNING: metrics render errored — accruing data without a .prom update." >&2

# Switch to the telemetry branch, creating it as a clean orphan ONLY when the
# ls-remote above proved it does not exist yet.
if [ "$BRANCH_EXISTS" = "1" ]; then
  git checkout -B "$BRANCH" FETCH_HEAD
  echo "Checked out existing $BRANCH."
else
  echo "Creating orphan $BRANCH."
  # A CI runner never has a local branch of this name, but a local re-run might —
  # --orphan refuses to reuse an existing name, so clear it first.
  git branch -D "$BRANCH" >/dev/null 2>&1 || true
  git checkout --orphan "$BRANCH"
  git rm -rf . >/dev/null 2>&1 || true
  : > "$DEST"
fi

# Refresh the on-branch README every run (not only at orphan creation), so a
# pre-existing branch still documents a newly-added data file like
# smoke-benchmark.jsonl. Idempotent: unchanged content stages to nothing.
printf '# Smoke test telemetry (accrued)\n\n`smoke-latency.jsonl` — one JSON line per (provider, feature) per run.\n`smoke-benchmark.jsonl` — one hardware-quality grade per healthy lease per run (optional).\n`smoke-metrics.prom` — the SAME data rendered as Prometheus textfile-collector metrics,\nre-rendered every run (fetch it raw and serve it to a Prometheus scrape).\n`deploy-credit.json` — last successful deploy-credit snapshot (gauge fallback).\nAll written by `.github/workflows/provider-smoke.yml`.\nAnalyze latency with `uv run python -m just_akash.analyze_telemetry smoke-latency.jsonl`.\n\nNEVER truncate or force-rewrite this branch casually: the .prom counters are\nre-derived cumulatively from these files, and a history rewrite reads as a\nPrometheus counter reset — increase()-based alert rules downstream would re-fire\non old outcomes for up to their full window. Pause the just-akash alert rules\nfirst if a rewrite is ever unavoidable.\n' > README.md

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

# Persist this run's credit snapshot so future runs (and their renders) can fall
# back to it when the snapshot step fails.
if [ -n "$CREDIT_SRC" ] && [ -s "$CREDIT_SRC" ]; then
  cp "$CREDIT_SRC" "$CREDIT_DEST"
  git add "$CREDIT_DEST"
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
