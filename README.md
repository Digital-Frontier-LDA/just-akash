# Smoke test latency telemetry (accrued)

One JSON line per (provider, feature) per run, appended by `.github/workflows/provider-smoke.yml`.
Seeded 2026-07-14 with a local concurrent batch (15 runs/provider).

Analyze from a checkout of `main`:
```
git show origin/telemetry:smoke-latency.jsonl > acc.jsonl
uv run python -m just_akash.analyze_telemetry acc.jsonl
```
