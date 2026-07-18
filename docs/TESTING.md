# Testing

Three tiers, deliberately separated. Knowing which tier a test belongs to is the
difference between a fast, honest CI signal and a slow, flaky one.

## 1. Unit tests — `tests/`, run on every push/PR

```bash
uv run pytest                       # the lot, with coverage
uv run pytest tests/test_deploy.py  # one file
uv run pytest -k phase2             # by name
uv run pytest --cov-report=html     # browse htmlcov/
```

- **No network, no credentials, no fixtures required.** The Console API and the
  WebSocket are mocked (`unittest.mock`, `FakeWebSocket` in
  `tests/test_lease_shell_exec.py`).
- **~1150 tests, ~92% coverage** of the unit-tested surface (the three live e2e
  scripts are omitted from the total — see §3).
- **Conventions:** one file per concern; `tests/test_<module>.py`. Adversarial and
  property-style edge cases are first-class (`test_adversarial.py`,
  `test_deploy.py`'s tiered-selection probes).

### Throwaway credentials — `tests/_creds.py`

`detect-secrets` anchors on *static* secret-looking literals. Test values are
generated at runtime so there is no literal to flag and the `.secrets.baseline`
doesn't churn on every edit:

```python
from tests._creds import fake_api_key, fake_secret
key = fake_api_key()        # 'test-key-<hex>' — a call, not a literal
secret = fake_secret()      # 'secret-<hex>'
```

Where a test asserts equality, capture the returned value rather than comparing to
a literal.

## 2. Local integration — `tests/test_integration_fake.py`, run on every push/PR

A localhost fake of the Console HTTP API **and** the provider-proxy WebSocket
(`tests/_fake_akash.py`), so the **full CLI runs end-to-end without credentials**:

```
real urllib HTTP  → localhost Console stub (deployments / bids / leases / jwt)
real websockets   → localhost provider-proxy stub (100/102/103 envelope frames)
```

`api._request`, the JWT fetch, `_decode_payload`, `_dispatch_frame`, and
`_pump_frames` all run unmodified. Only three production TLS guards are bypassed
(each is itself unit-tested): `_get_proxy_ws_url` is redirected to the local
`ws://` stub, `connect()`'s mandatory `ssl=` is stripped for `ws://`, and the stub
`console_url` is injected (it defaults to the real Console).

Use it for behavior that mocks can't catch — the real frame round-trip, the
inject→readback secrets path, the closed-without-result regression:

```python
def test_inject_then_cat_round_trip(self, fake_akash, capsys):
    _run_cli(["just-akash", "inject", "--dseq", "1234", "--env", "SECRET=s3cret"])
    _run_cli(["just-akash", "exec", "--dseq", "1234", "cat /run/secrets/.env"])
    assert "s3cret" in capsys.readouterr().out
```

The `FakeShell` interpreter is deliberately tiny (echo / cat / mkdir / chmod / sh -c
inject-write). Extend it — don't reach for a real container — when you need a new
command shape.

## 3. Live e2e — `just_akash/test_*.py`, run via `just` / CI e2e jobs

These deploy **real** Akash leases and spend real uAKT. They are **not** collected
by pytest; they run as scripts.

| Recipe | Module | Flow |
|---|---|---|
| `just test` | `test_lifecycle.py` | up → list → status → SSH connect → destroy |
| `just test-secrets` | `test_secrets_e2e.py` | deploy SSH instance → inject via SSH → verify value + `chmod 600` |
| `just test-shell` | `test_shell_e2e.py` | deploy → exec/inject via lease-shell → cross-verify via SSH |

All three share `just_akash/_e2e.py`:

- **`robust_destroy`** — leak-proof teardown (retry + audit that reads the per-deployment
  status, not the stale `just list`). Unit-pinned at 100% by `tests/test_e2e_cleanup.py`.
- **`install_signal_cleanup`** — SIGINT/SIGTERM handler that destroys every registered
  dseq. Register the ref dict *before* deploying.
- **`resolve_tiers` / `assert_provider_in_tiers`** — preferred ∪ backup allowlist check.

They require `AKASH_API_KEY`, `AKASH_PROVIDERS`, `SSH_PUBKEY` (CI secrets). The e2e
jobs serialize (same API key = same account; concurrent deploys collide).

### Why the coverage number excludes them

The three scripts (~540 statements) can't contribute coverage — pytest never runs
them. `[tool.coverage.run] omit` keeps them out of the total so the reported %
reflects only the unit-tested surface. Their *behavior* is covered: the `_e2e.py`
helpers are at 100%, and the scripts run live in CI.

## CI layout (`.github/workflows/`)

- **`ci.yml`** — lint (ruff), typecheck (pyright), unit tests, e2e-shell, e2e-secrets.
- **`provider-smoke.yml`** — daily provider capability matrix + telemetry accrual.
- **`security.yml` / `secrets.yml`** — gitleaks / trufflehog / detect-secrets / semgrep / pip-audit.

## Writing a good test here

- **Prefer real behavior over mock verification.** Assert what the system *did*
  (the SDL submitted, the exit code, the stdout), not that a mock was called with
  expected args — the latter encodes the mock as the oracle.
- **No `assert True`.** If a test can only prove "didn't crash," say so in a comment
  and assert the real post-condition where possible (see the image-override tests in
  `test_adversarial.py`).
- **Tie regressions to issues** (`#14`, `#12`, `#39`) in the docstring so the why
  survives the fix.
