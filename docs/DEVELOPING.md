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

**This is enforced, not just convention.** `.github/scripts/check_repo_invariants.py`
fails CI if any workflow or composite action references a GitHub secret other than
`SOPS_AGE_KEY` (or `GITHUB_TOKEN`, which is auto-provisioned per job and could never
live in SOPS). The migration was eroded twice by PRs that re-added a direct
`secrets.AKASH_API_KEY` — a direct reference is ordinary-looking YAML and it *works*,
so nothing surfaced it until someone re-read the workflows. The same script checks
that `CHANGELOG.md` versions are unique, strictly descending, and match
`pyproject.toml` (a merge once left two `## [1.37.0]` sections). Run it locally with:

```bash
python3 .github/scripts/check_repo_invariants.py
```

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

## Typed execution observation

```python
from akash_lease_core import DeploymentKey, PreparedGroup
from just_akash.execution_observation import observe_execution

observation = observe_execution(
    operation_id,
    DeploymentKey(owner, dseq),
    tuple(PreparedGroup(index, name) for index, name in enumerate(signed_create_names, 1)),
)
```

The caller supplies the exact operation, deployment and complete expected group
population from its known create receipt. The adapter independently binds that
population to the successful signed create on the two registered chain sources;
caller metadata or a Console response does not establish ownership.

`no_further_close_needed` is true only for a complete, fresh, finalized closed
execution snapshot: deployment, every expected group, and every enumerated lease.
A false/unknown observation never grants permission to submit a close. Escrow is
reported separately as `settled`, `overdrawn-unsettled`, or `unknown`.

`closure` contains the pinned core `ExecutionClosure` only when the real successful
signed `MsgCloseDeployment` height is recovered. Every historical state probe is
height pinned and corroborated; the exact transition block is completely exhausted,
its raw transactions and decoded population agree, and the close transaction hash
is read back from both sources. A complete finalized snapshot is refreshed afterward.
Unavailable history leaves `closure=None` and preserves the dated closed snapshot
for replay suppression. `recover_close_transaction=False` requests that snapshot
without historical close recovery.

`settlement` is a separate core `SettlementEvidence` with `UNMEASURED` payment
settlement. Closed escrow alone is not a complete payment proof; the explicit
`payment_settlement_proven` and `financial_exposure_release_authorized` properties
remain false for every observation. Neither result mutates a journal, authorizes
retirement, nor releases financial exposure. A consumer
must retain its ownership/admission checks and require typed closure before appending
closure-dependent journal/accounting transitions. Unknown or overdrawn escrow cannot
release financial exposure. The legacy `_lease_verification.verdict` meaning remains
unchanged. Default reads carry no Console credentials, refuse redirects and missing
height echoes, and enforce byte/read/time bounds and strict JSON decoding.

## Release flow

1. Bump `version` in `pyproject.toml` and add a unique, descending
   `## [x.y.z] — YYYY-MM-DD` entry to `CHANGELOG.md`.
2. Review and merge the exact release source through the required main checks.
   Wait for its post-merge `CI` (including both live E2E jobs), `Secret Scan`, and
   `Security` push runs to finish successfully. A merge-queue or PR result on another
   commit is insufficient.
3. Tag that reviewed main ancestor as `v<project version>` and push the tag after
   release authorization. The `Release` workflow reads the tag source and refuses
   publication unless the tag/version agree, the commit is an ancestor of current
   main, and every expected gate succeeded in the latest exact-commit push runs.
   Immediately before publication it resolves the remote tag again (including
   annotated tags), rechecks main ancestry and the exact recorded successful CI
   run attempts, and refuses moved tags or newly started reruns. These read fences
   cannot make tag movement atomic with release creation; release tags must remain
   unchanged. It does not dispatch paid tests or skip failed gates. A premature tag run can be
   rerun after the exact source gates pass; source must never be changed under a tag.
4. The workflow builds with the closed pinned backend/tool set and `--no-isolation`,
   checks `just_akash-<version>-py3-none-any.whl`, imports the typed API from the built
   wheel with the already pinned core release, verifies the financial DATA codec,
   then uploads wheel and sdist to a new
   immutable GitHub release. Existing releases are refused.
5. Copy the emitted SHA-256 requirements line into each consumer, update its lock/pin,
   and validate the installed released wheel through the actual consumer path.
   SDK 1.47.0 consumes the published core v0.17.0 wheel (SHA-256
   `a3e41338835d6929ad90c8117107cd59a5c04fde11392259a34c0323aead353d`).
   Its financial records and codecs are data, never quote, signing or sending
   authority. Genuine policy, issuer, durable CAS/ACK and custody integration remain
   separate prerequisites. Earlier SDK 1.44–1.46 releases retain their immutable
   core v0.16.1 pin; the original execution adapter required no core change.
   Consumer live recovery and observation-window acceptance remain separate evidence.
   A dependency or authenticated source-bundle change requires newly authenticated
   history and a new actual cleanup observation window; no dated proof is refreshed
   by changing its timestamp.

Do not republish v1.43.1: its published wheel predates the current journal/admission
interfaces. The additive execution API is released as v1.44.0.

## Conventions worth preserving

- **Defensive reads of Console payloads.** Every field from the API is
  `isinstance`-guarded — the Console shapes drift, and a stray `None`/list must not
  crash the CLI.
- **Atomic writes** for local state (`_save_tags` uses `tempfile` + `os.replace`).
- **Comments that explain *why*, not what.** The codebase is dense with load-bearing
  comments tied to issues (`# issue #14`, `# AEP-64`); keep that discipline.
- **No `shell=True` on user input.** SSH argv lists are built explicitly and
  `shlex.quote`d; `S602` stays enabled to enforce it.
