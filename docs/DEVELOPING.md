# Developing

Contributor workflow. `CONTRIBUTING.md` covers the social contract (issues, PRs,
secrets); this covers the technical setup and how to extend the tool.

## Setup

```bash
git clone https://github.com/Digital-Frontier-LDA/just-akash
cd just-akash
cp .env.example .env            # add AKASH_API_KEY, providers, SSH_PUBKEY
uv sync --dev                   # package + dev tools
uv run pre-commit install       # gitleaks + ruff + detect-secrets hooks
```

Python ≥ 3.10 (`pyproject.toml`; CI runs 3.13). The project is **minimal-dependency
at runtime** — only `websockets`, `pexpect`, `pyyaml` — so don't add a runtime
dependency without
discussing it.

## Secrets (SOPS + age)

CI secrets live **encrypted in git** at `secrets/ci.sops.env`, not in a pile of
GitHub secrets. The only GitHub secret is `SOPS_AGE_KEY` — the bootstrap key CI
uses to decrypt everything else.

```bash
sops -d secrets/ci.sops.env      # read (decrypts to stdout)
sops secrets/ci.sops.env         # edit in $EDITOR, re-encrypts on save
```

Recipients are in `.sops.yaml`: **ops** (laptop), **breakglass** (1Password), and
**ci-just-akash** (the `SOPS_AGE_KEY` secret on this repo). Per-repo CI keys mean a
compromised CI key exposes only this repo. After changing recipients, re-encrypt
with `sops updatekeys secrets/ci.sops.env`.

Workflows load them via a composite action that decrypts to a private tmpfile,
masks every value, then exports to `$GITHUB_ENV`:

```yaml
- name: Load SOPS secrets
  uses: ./.github/actions/sops-env
  with:
    age-key: ${{ secrets.SOPS_AGE_KEY }}
```

**Adding a secret:** `sops secrets/ci.sops.env`, add `KEY=value`, save, commit. Any
job with the load step picks it up — no GitHub-secret change, and the addition is
reviewable in the PR diff.

Local development still uses a plain untracked `.env` (see `.env.example`).
`.gitignore` blocks `secrets/*.env` so a decrypted sibling can never be committed;
only `*.sops.env` is trackable.

## Quality recipes (`just`)

| Recipe | Does | Spend? |
|---|---|---|
| `just lint` | ruff check + format check | no |
| `just typecheck` | pyright | no |
| `just fmt` / `just check` | ruff format / `--fix` | no |
| `just secrets` | gitleaks scan | no |
| `just semgrep` | SAST (p/python + p/security-audit) | no |
| `just audit` | pip-audit dependency CVEs | no |
| `just test` / `test-secrets` / `test-shell` | live e2e (real leases) | **yes — uAKT** |
| `just smoke-providers` | provider fleet capability matrix | **yes — uAKT** |
| `just smoke-telemetry-report` | grade accrued telemetry | no |

Run the no-spend checks before every push; they're what CI runs. Before merging,
`just lint && just typecheck && uv run pytest` must be green.

## Test workflow

See `TESTING.md`. Short version:

```bash
uv run pytest                           # unit + local integration, with coverage
uv run pytest tests/test_integration_fake.py   # the local fake-Akash suite
uv run pytest -k benchmark              # by name
```

## Adding a CLI command

The `benchmark` subcommand (`cli.py`) is the canonical template — it wires a
transport operation through dispatch. The pattern:

1. **argparse subparser** in `cli.main` (`bench_p = subparsers.add_parser(...)`).
2. **Dispatch branch** — `elif args.command == "benchmark":`. Build a client, resolve
   the dseq, build a transport via `make_transport`, validate, call the transport
   method, print, `sys.exit(rc)`.
3. **Wrap `RuntimeError`** → `print(f"Error: {e}", file=sys.stderr); sys.exit(1)` so
   API/transport failures surface as exit 1 with a message, not a traceback.
4. **Test it through `cli.main`** (`tests/test_cli_dispatch.py`) — drive the dispatch
   body, not just the underlying helper. The `benchmark` stdout-capture trick and the
   `inject` `--env-file` parsing are exactly the kind of thing that regresses without a
   dispatch test.

## Adding a transport

1. Implement the `Transport` ABC (`transport/base.py`): `prepare / exec / inject /
   connect / validate`.
2. Register it in `transport/__init__.py`'s `make_transport` factory.
3. Add a `--transport <name>` choice where relevant (`cli.py` connect/exec/inject).
4. Test the frame/protocol surface; add a case to the local fake suite
   (`tests/_fake_akash.py`) if it has a new wire shape.

## Release flow

1. Bump `version` in `pyproject.toml`.
2. Add a `## [x.y.z] — YYYY-MM-DD` entry to `CHANGELOG.md` (Keep a Changelog). Be
   candid — the changelog documents failures and reversions, not just features.
3. Commit, tag, push. CI runs the no-spend + (on `main`) the e2e jobs.

## Conventions worth preserving

- **Defensive reads of Console payloads.** Every field from the API is
  `isinstance`-guarded — the Console shapes drift, and a stray `None`/list must not
  crash the CLI.
- **Atomic writes** for local state (`_save_tags` uses `tempfile` + `os.replace`).
- **Comments that explain *why*, not what.** The codebase is dense with load-bearing
  comments tied to issues (`# issue #14`, `# AEP-64`); keep that discipline.
- **No `shell=True` on user input.** SSH argv lists are built explicitly and
  `shlex.quote`d; `S602` stays enabled to enforce it.
