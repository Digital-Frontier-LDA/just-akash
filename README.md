# Smoke test telemetry (accrued)

`smoke-latency.jsonl` — one JSON line per (provider, feature) per run.
`smoke-benchmark.jsonl` — one hardware-quality grade per healthy lease per run (optional).
Both appended by `.github/workflows/provider-smoke.yml`.
Analyze latency with `uv run python -m just_akash.analyze_telemetry smoke-latency.jsonl`.
