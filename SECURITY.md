# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report vulnerabilities privately by emailing: **jonathanborduas@gmail.com**

Include:
- A description of the vulnerability
- Steps to reproduce
- Potential impact

You will receive a response within 72 hours. If the issue is confirmed, a fix will be prioritised and a patched release published as soon as possible. You will be credited in the release notes unless you prefer otherwise.

## Scope

This tool deploys compute instances on Akash Network via the Console API. The main security surface areas are:

- **`.env` file** — contains your Akash Console API key and provider allowlist. Gitignored. Never commit it.
- **`AKASH_API_KEY`** — your Console API key. Read from environment, never hardcoded.
- **`SSH_PUBKEY`** — injected into containers at deploy time. Not stored in the repo.
- **Provider allowlist** — controls which providers can host your workloads. Keep it restricted to providers you trust.

## Secret Scanning

This repository uses three layers of secret detection:
- **Gitleaks** — pre-commit hook + CI on every push/PR + weekly full-history scan
- **TruffleHog** — CI on every push/PR (verified secrets only)
- **detect-secrets** — baseline file + CI diff check

If you find a secret in the repository, report it immediately using the process above.

## Static Analysis & Dependency Auditing

In addition to secret scanning, the repository runs:

- **Ruff bandit rules (`S`)** — Python security SAST on every push/PR (the `lint`
  CI job). Catches `shell=True` (`S602`), unsafe temp files, weak SSL, etc.
  `assert`-based tests and shell-based e2e orchestration are scoped out via
  per-file ignores. `S603`/`S606`/`S607` (subprocess/exec mechanics) are
  per-file-ignored only for the modules that invoke `ssh` by design
  (`just_akash/api.py`, `cli.py`, `transport/ssh.py`) — they build argv lists
  (never `shell=True`) and resolve `ssh` from `PATH`. Scoping them per-file
  (rather than globally) keeps a new unsafe subprocess elsewhere failing CI.
- **Semgrep** (`p/python` + `p/security-audit`) — the `Semgrep SAST` security job
  (`just semgrep`). Two rules are excluded because they are inherent to a
  remote-exec CLI and the underlying risk is mitigated in code:
    - `dangerous-subprocess-use-tainted-env-args` — the tool runs commands and
      writes files on the **user's own** deployment by design; user-supplied
      paths are passed through `shlex.quote`.
    - `dynamic-urllib-use-detected` — the request URL is built from the
      operator-set Console API base URL, not external/attacker input.
  `subprocess-shell-true` (real shell-injection) and all other rules stay active.
- **pip-audit** — dependency CVE audit on every push/PR and weekly (`just audit`),
  failing the build on any known vulnerability in a locked dependency.
