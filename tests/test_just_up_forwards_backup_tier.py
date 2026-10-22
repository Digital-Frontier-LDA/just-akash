"""`just up` forwards an explicit backup tier (CodeRabbit, PR #475): deploy no longer reads
AKASH_PROVIDERS_BACKUP, so the E2E callers pass their tier in JUST_UP_BACKUP_PROVIDERS and the
recipe turns each entry into a --backup-provider flag."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_up_recipe_forwards_each_backup_provider_as_a_flag():
    text = (ROOT / "Justfile").read_text()
    up = text[text.index("\nup ") : text.index("\n# Connect")]
    assert "JUST_UP_BACKUP_PROVIDERS" in up and 'args+=(--backup-provider "${p// /}")' in up
    assert "AKASH_PROVIDERS_BACKUP" not in up.replace("reads AKASH_PROVIDERS_BACKUP", ""), (
        "the recipe must not re-create the env inheritance"
    )


def test_both_e2e_callers_pass_their_backup_tier_explicitly():
    for f in ("just_akash/test_secrets_e2e.py", "just_akash/test_shell_e2e.py"):
        src = (ROOT / f).read_text()
        assert re.search(r'receipt_env\["JUST_UP_BACKUP_PROVIDERS"\] = ",".join\(backup\)', src), f


def test_the_forwarding_loop_runs_under_bash_3_and_set_u():
    """macOS /bin/bash is 3.2, where an empty array under `set -u` is "unbound": the default
    (no backup tier) must not abort `just up`."""
    import shutil
    import subprocess

    import pytest

    bash = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    text = (ROOT / "Justfile").read_text()
    up = text[text.index("\nup ") : text.index("\n# Connect")]
    loop = up[
        up.index('    if [ -n "${JUST_UP_BACKUP_PROVIDERS:-}" ]; then') : up.index(
            '    "${args[@]}" | tee'
        )
    ]
    script = "set -euo pipefail\nargs=(deploy)\n" + loop + '\nprintf "%s\\n" "${args[@]}"\n'
    for value, want in (
        ("", ["deploy"]),
        (",", ["deploy"]),
        (
            "akash1a, akash1b",
            ["deploy", "--backup-provider", "akash1a", "--backup-provider", "akash1b"],
        ),
    ):
        out = subprocess.run(
            [bash, "-c", script],
            env={"JUST_UP_BACKUP_PROVIDERS": value, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, (value, out.stderr)
        assert out.stdout.split() == want, (value, out.stdout)
