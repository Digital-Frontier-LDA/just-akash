"""Every paid-deployment cleanup site in the secrets E2E has an observable effect (#358).

`just_akash/test_secrets_e2e.py` closes the paid lease it created from four places besides
`_finish`: an interrupted `just up`, a failed or timed-out `just up`, a create whose only
DSEQ came from reconciliation, and the final `finally`. Removing any of them left every
test green, so a paid lease could be left open with CI passing.

Each test below runs the REAL `main()` down one of those branches:

- `just up` is stubbed. It writes a REAL durable create receipt, using the same
  `deployment_receipt` functions `deploy` uses, at the path and operation ID `main()`
  hands it.
- `robust_destroy` is stubbed. Each call records the exact `(dseq, owner, groups, credential)` and
  whether the receipt still existed at that moment, so "removed only after verified
  closure" is observed, not inferred.
- Only the branches that need them stub anything further: the two chain observers that
  reconciliation reads (:355), and the offline shell-outs and clock that the final
  cleanup (:571) runs through (`uv run just-akash status/inject` and `ssh`).
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
from pathlib import Path

import pytest

from just_akash import _e2e
from just_akash import paid_create as paid
from just_akash import test_secrets_e2e as target
from just_akash.deployment_receipt import (
    CredentialBinding,
    mark_create_response_received,
    mark_submitting,
    prepare_receipt,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
PROVIDER = "akash1provider0000000000000000000000000000000"
GROUP = "just-akash-secrets.abc123def456"
GROUPS = [{"gseq": 1, "name": GROUP}]
# deploy() writes the creating credential's position into the receipt (#363); every cleanup
# site must forward it to robust_destroy as the lookup-order hint.
BINDING: CredentialBinding = {"credential_index": 0, "credential_count": 1}
SDL = f"""---
version: "2.0"
services:
  app:
    image: example.invalid/image:latest
    expose: []
profiles:
  compute:
    app:
      resources:
        cpu: {{units: 1}}
        memory: {{size: 1Gi}}
        storage: {{size: 1Gi}}
  placement:
    {GROUP}:
      pricing:
        app: {{denom: uakt, amount: 1}}
deployment:
  app:
    {GROUP}:
      profile: app
      count: 1
"""


@pytest.fixture
def e2e(monkeypatch, tmp_path: Path):
    """The environment main() validates, a private RUNNER_TEMP for the receipt, and a
    record of every destroy. Signal handlers installed by main() are restored after."""
    for var, value in (
        ("AKASH_API_KEY", "stub"),  # pragma: allowlist secret
        ("AKASH_PROVIDERS", PROVIDER),
        ("SSH_PUBKEY", "ssh-ed25519 stub"),
        ("SSH_KEY_PATH", str(tmp_path / "id_stub")),
        ("RUNNER_TEMP", str(tmp_path)),
    ):
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    state: dict = {"receipt": None, "destroys": [], "verdicts": []}

    def destroy(label: str):
        def record(dseq, **kwargs):
            receipt = state["receipt"]
            state["destroys"].append(
                (label, dseq, kwargs, receipt is not None and receipt.exists())
            )
            return state["verdicts"].pop(0)

        return record

    monkeypatch.setattr(target, "robust_destroy", destroy("main"))
    monkeypatch.setattr(paid, "robust_destroy", destroy("reconcile"))
    yield state
    _e2e._reset_signal_cleanup_for_tests()
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def _just_up(monkeypatch, state: dict, *, dseq: str | None, outcome):
    """Stub `just up`: write the real receipt, then return or raise as `outcome` says."""

    def just_up(env: dict, timeout: int = 300):
        path = Path(env["JUST_AKASH_RECEIPT_PATH"])
        state["receipt"] = path
        submitting = mark_submitting(
            *prepare_receipt(
                str(path),
                operation_id=env["JUST_AKASH_RECEIPT_OPERATION_ID"],
                owner=OWNER,
                sdl_content=SDL,
                credential_binding=BINDING,
            )
        )
        if dseq is not None:
            mark_create_response_received(
                *submitting, dseq=dseq, deployment_response={"dseq": dseq}
            )
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, timed_out = outcome
        return (
            subprocess.CompletedProcess(["just", "up"], returncode, f"DSEQ: {dseq}", ""),
            timed_out,
        )

    monkeypatch.setattr(target, "_run_just_up", just_up)


def _destroyed_once(state: dict, label: str, dseq: str) -> None:
    assert [(d[0], d[1], d[2]) for d in state["destroys"]] == [
        (label, dseq, {"owner": OWNER, "groups": GROUPS, "credential": BINDING})
    ], state["destroys"]
    assert state["destroys"][0][3], "the receipt was removed before the verified closure"


# ── :309 — interrupted `just up` after the receipt recorded a DSEQ ──────────────────────


def test_an_interrupted_create_closes_its_receipt_dseq(monkeypatch, e2e) -> None:
    e2e["verdicts"] = [True]
    _just_up(monkeypatch, e2e, dseq="1001", outcome=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        target.main()
    _destroyed_once(e2e, "main", "1001")
    assert not e2e["receipt"].exists(), "a verified closure must consume its receipt"


# ── :349 — `just up` failed or timed out after the receipt recorded a DSEQ ──────────────


@pytest.mark.parametrize(
    ("outcome", "label"),
    [((1, False), "failure"), ((0, True), "timeout")],
    ids=["failure", "timeout"],
)
def test_a_failed_or_timed_out_create_closes_its_receipt_dseq(
    monkeypatch, e2e, outcome, label
) -> None:
    e2e["verdicts"] = [True]
    _just_up(monkeypatch, e2e, dseq="1001", outcome=outcome)
    with pytest.raises(SystemExit) as exit_info:
        target.main()
    assert exit_info.value.code == 1, label
    _destroyed_once(e2e, "main", "1001")
    assert not e2e["receipt"].exists()


def test_an_unverified_close_after_a_failed_create_keeps_the_receipt(monkeypatch, e2e) -> None:
    """Opposite leg: the receipt is the recovery seed until closure is verified."""
    e2e["verdicts"] = [False]
    _just_up(monkeypatch, e2e, dseq="1001", outcome=(1, False))
    with pytest.raises(SystemExit):
        target.main()
    _destroyed_once(e2e, "main", "1001")
    assert e2e["receipt"].exists()


# ── :355 — no response DSEQ, but reconciliation identified one it could not close ───────


def test_a_reconciled_dseq_the_reconciler_could_not_close_is_closed_by_main(
    monkeypatch, e2e
) -> None:
    started = int(time.time() * 1000)
    reconciled = str(started + 60_000)
    monkeypatch.setattr(
        paid.chain, "list_active_deployments", lambda owner: [{"dseq": reconciled}]
    )
    monkeypatch.setattr(
        paid.chain,
        "corroborated_deployment_group_population",
        lambda owner, dseq, population: population,
    )
    e2e["verdicts"] = [False, True]  # the reconciler's close fails; main's retry verifies
    _just_up(monkeypatch, e2e, dseq=None, outcome=(0, False))
    with pytest.raises(SystemExit) as exit_info:
        target.main()
    assert exit_info.value.code == 1
    calls = [(d[0], d[1], d[2]) for d in e2e["destroys"]]
    assert calls == [
        ("reconcile", reconciled, {"owner": OWNER, "groups": GROUPS, "credential": BINDING}),
        ("main", reconciled, {"owner": OWNER, "groups": GROUPS, "credential": BINDING}),
    ], calls
    assert all(d[3] for d in e2e["destroys"]), "the receipt was removed before closure"
    assert not e2e["receipt"].exists()


# ── :571 — the final cleanup after every step ran ────────────────────────────────────────


def _offline_provider(monkeypatch) -> None:
    """The instance main() talks to after deploy: `just-akash status/inject` shell-outs
    and `ssh`, simulated in memory so every step passes and main() reaches its `finally`."""
    files: dict[str, str] = {}

    def run(argv: list[str], timeout: int = 60, input_text: str | None = None):
        assert isinstance(argv, list), f"run() must receive argv, got {argv!r}"  # #371
        if argv[:4] == ["uv", "run", "just-akash", "status"]:
            body = json.dumps(
                {"ssh_host": "instance.example", "ssh_port": 2222, "provider": PROVIDER}
            )
            return subprocess.CompletedProcess(argv, 0, body, "")
        if argv[:4] == ["uv", "run", "just-akash", "inject"]:
            env_file = argv[argv.index("--env-file") + 1]
            remote = argv[argv.index("--remote-path") + 1] if "--remote-path" in argv else None
            files[remote or "/run/secrets/.env"] = Path(env_file).read_text()
            return subprocess.CompletedProcess(argv, 0, "Injected 2 variable(s)", "")
        raise AssertionError(f"unexpected command: {argv}")

    def ssh(argv, **_kwargs):
        remote = argv[-1]
        if remote == "echo akash-ssh-ok":
            out = "akash-ssh-ok"
        elif remote.startswith("cat "):
            out = files.get(remote[4:], "")
        elif remote.startswith("stat "):
            out = "600"
        else:
            raise AssertionError(f"unexpected ssh command: {remote}")
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(target, "run", run)
    monkeypatch.setattr(target.subprocess, "run", ssh)
    monkeypatch.setattr(target.time, "sleep", lambda _seconds: None)


def test_the_final_cleanup_closes_the_lease_after_a_passing_run(monkeypatch, e2e) -> None:
    _offline_provider(monkeypatch)
    e2e["verdicts"] = [True]
    _just_up(monkeypatch, e2e, dseq="1001", outcome=(0, False))
    with pytest.raises(SystemExit) as exit_info:
        target.main()
    assert exit_info.value.code == 0, "every simulated step should pass"
    _destroyed_once(e2e, "main", "1001")
    assert not e2e["receipt"].exists()


def test_an_unverified_final_closure_is_a_failure(monkeypatch, e2e, capsys) -> None:
    _offline_provider(monkeypatch)
    e2e["verdicts"] = [False]
    _just_up(monkeypatch, e2e, dseq="1001", outcome=(0, False))
    with pytest.raises(SystemExit) as exit_info:
        target.main()
    assert exit_info.value.code != 0, "an unverified closure of a paid lease must fail the run"
    assert "cleanup: destroy or audit failed" in capsys.readouterr().out
    _destroyed_once(e2e, "main", "1001")
    assert e2e["receipt"].exists(), "an unverified closure must keep the recovery receipt"
