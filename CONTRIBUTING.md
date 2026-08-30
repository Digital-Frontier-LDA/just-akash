# Contributing

Contributions are welcome — bug reports, fixes, and well-scoped features.

## Before you start

For anything beyond a small bug fix, open an issue first to discuss the change. This avoids wasted effort if the direction doesn't fit the project.

## Setup

```bash
git clone https://github.com/jobordu/just-akash
cd just-akash
cp .env.example .env
# Edit .env — add your API key, providers, SSH pubkey
```

## Running the tests

```bash
just unit          # unit + integration suite — no deployment, no spend, ~65s
just lint          # ruff lint + format check
just typecheck     # pyright
```

⛔ **Use `uv run`, never a bare `python3 -m pytest`.** `akash-lease-core` is a PEP 508
direct reference to a release wheel URL (see `pyproject.toml`), so it is installed into the
uv environment and nowhere else. Run the suite outside that environment and it reports
about **145 failures — 98 `ModuleNotFoundError` and 37 `AttributeError`** — which reads
exactly like a broken repository and is in fact one absent dependency.

Measured 2026-08-30 on the same tree in the same minute:

| invocation | result |
|---|---|
| `python3 -m pytest tests/` | **145 failed**, 27 collection errors |
| `uv run pytest tests/` | **0 failed** |

⚠ Only the FAILURE counts are given. A passing total drifts with every test added and would
be stale within a week — a hardcoded number in documentation rots exactly like one in code,
and the point here is the 145-vs-0, not the size of the suite.

⚠ The failures do not name the cause in a way that points at the environment. A dependency
that is *declared* but absent produces the same `ModuleNotFoundError` as one that was never
declared, and the two call for opposite fixes — "add it to pyproject and open a PR" versus
"re-run under the environment that already has it". Check which interpreter you are using
before concluding anything about the code:

```bash
uv run python -c "import akash_lease_core, sys; print('OK', sys.executable)"
python3      -c "import akash_lease_core, sys; print('OK', sys.executable)"
```

⚠ **`just test` is not this.** It runs the full lifecycle E2E — it deploys real
infrastructure and spends escrow. `just unit` is the one you want.

## Workflow

```bash
git checkout -b fix/your-change   # or feature/your-change
# make changes
just lint                          # ruff lint + format check
just secrets                       # gitleaks secret scan
git commit -m "..."
git push origin fix/your-change
# open a PR against main
```

## Guidelines

- Keep changes focused. One concern per PR.
- Do not commit `.env` or `.tags.json` — both are gitignored for good reason.
- Run `just secrets` before pushing to confirm no secrets are staged.
- All PRs run the gitleaks secret scan CI check. It must pass before merging.
- Write clear commit messages. Prefer the imperative mood ("Add X", "Fix Y", "Remove Z").

## What fits this project

- Bug fixes
- New deployment strategies or bid selection logic
- Improved provider diagnostics
- Better SDL templates
- Documentation improvements

## What doesn't fit

- Adding external Python dependencies (this tool is intentionally stdlib-only)
- Storing API keys or credentials anywhere other than environment variables
- Changes that require a specific cloud provider beyond Akash Network

## Reporting bugs

Use the [Bug Report](.github/ISSUE_TEMPLATE/bug_report.md) issue template. Include the deployment log output — the structured JSON lines make diagnosis much faster.
