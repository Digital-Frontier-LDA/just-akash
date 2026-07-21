# Smoke test telemetry (accrued)

`smoke-latency.jsonl` — one JSON line per (provider, feature) per run.
`smoke-benchmark.jsonl` — one hardware-quality grade per healthy lease per run (optional).
`smoke-metrics.prom` — the SAME data rendered as Prometheus textfile-collector metrics,
re-rendered every run (fetch it raw and serve it to a Prometheus scrape).
`deploy-credit.json` — last successful deploy-credit snapshot (gauge fallback).
All written by `.github/workflows/provider-smoke.yml`.
Analyze latency with `uv run python -m just_akash.analyze_telemetry smoke-latency.jsonl`.

NEVER truncate or force-rewrite this branch casually: the .prom counters are
re-derived cumulatively from these files, and a history rewrite reads as a
Prometheus counter reset — increase()-based alert rules downstream would re-fire
on old outcomes for up to their full window. Pause the just-akash alert rules
first if a rewrite is ever unavoidable.
