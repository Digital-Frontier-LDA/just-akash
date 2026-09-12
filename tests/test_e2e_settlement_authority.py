"""Owner binding and multi-reader settlement audit for E2E cleanup."""

from __future__ import annotations

import inspect
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from just_akash import _e2e

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
DSEQ = "1789233446929"
ROOT = Path(__file__).resolve().parents[1]
E2E_CALLERS = (
    "just_akash/test_lifecycle.py",
    "just_akash/test_secrets_e2e.py",
    "just_akash/test_shell_e2e.py",
    "just_akash/smoke_providers.py",
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_owner_is_resolved_and_validated_before_destroy():
    events: list[tuple[str, object]] = []

    def resolve(dseq):
        events.append(("resolve", dseq))
        return OWNER

    def destroy(dseq, **kwargs):
        events.append(("destroy", (dseq, kwargs)))
        return True

    with (
        patch.object(_e2e, "resolve_deployment_owner", side_effect=resolve),
        patch.object(_e2e, "robust_destroy", side_effect=destroy),
    ):
        assert _e2e.destroy_owned_deployment(DSEQ) is True

    assert events == [
        ("resolve", DSEQ),
        ("destroy", (DSEQ, {"owner": OWNER, "retries": 2, "audit": True})),
    ]


def test_owner_resolution_failure_holds_before_destroy():
    with (
        patch.object(_e2e, "resolve_deployment_owner", side_effect=RuntimeError("unreadable")),
        patch.object(_e2e, "robust_destroy") as destroy,
    ):
        assert _e2e.destroy_owned_deployment(DSEQ) is False
    destroy.assert_not_called()


def test_resolver_rejects_a_response_for_another_dseq():
    payload = f'{{"owner": "{OWNER}", "dseq": "999", "source": "wallet_pool"}}'
    with patch.object(_e2e, "_run", return_value=_completed(0, payload)):
        try:
            _e2e.resolve_deployment_owner(DSEQ)
        except RuntimeError as exc:
            assert "requested DSEQ" in str(exc)
        else:
            raise AssertionError("a mismatched owner/DSEQ response must be rejected")


def test_resolver_accepts_only_typed_owner_binding():
    payload = f'{{"owner": "{OWNER}", "dseq": "{DSEQ}", "source": "wallet_pool"}}'
    with patch.object(_e2e, "_run", return_value=_completed(0, payload)) as run:
        assert _e2e.resolve_deployment_owner(DSEQ) == OWNER
    assert run.call_args.kwargs["timeout"] == 60

    unknown_source = payload.replace("wallet_pool", "guess")
    with patch.object(_e2e, "_run", return_value=_completed(0, unknown_source)):
        try:
            _e2e.resolve_deployment_owner(DSEQ)
        except RuntimeError as exc:
            assert "evidence source" in str(exc)
        else:
            raise AssertionError("an untyped owner source must be rejected")


def test_checksum_invalid_owner_is_held_before_destroy():
    invalid_owner = OWNER[:-1] + ("q" if OWNER[-1] != "q" else "p")
    assert invalid_owner.startswith("akash1") and len(invalid_owner) == len(OWNER)
    with patch.object(_e2e, "_run") as run:
        assert _e2e.robust_destroy(DSEQ, owner=invalid_owner) is False
    run.assert_not_called()


def test_typed_multireader_verdict_is_the_settlement_effect():
    verdict = Mock(return_value={"closed": True, "sources": ["a", "b"]})
    with patch.object(_e2e, "closure_verdict", verdict):
        assert _e2e._confirm_settled(DSEQ, OWNER, attempts=6, interval_s=2) is True
    assert verdict.call_args.args[:3] == (DSEQ, OWNER, _e2e.DEFAULT_ENDPOINTS)
    assert verdict.call_args.kwargs == {"retries": 6, "retry_sleep_s": 2}

    verdict.return_value = {"closed": False, "sources": ["a", "b"]}
    with patch.object(_e2e, "closure_verdict", verdict):
        assert _e2e._confirm_settled(DSEQ, OWNER) is False


def test_laggy_single_reader_cannot_emit_still_active(capsys):
    with (
        patch.object(_e2e, "_run", return_value=_completed(0, "Deployment destroyed")),
        patch.object(_e2e, "_confirm_settled", return_value=False),
        patch.object(_e2e.time, "sleep"),
    ):
        assert _e2e.robust_destroy(DSEQ, owner=OWNER) is False
    output = capsys.readouterr().out
    assert "settlement not observed" in output
    assert "STILL ACTIVE" not in output


def test_timeout_reports_measured_elapsed_time_not_a_stale_constant(capsys):
    with (
        patch.object(_e2e, "_run", return_value=_completed(0, "Deployment destroyed")),
        patch.object(_e2e, "_confirm_settled", return_value=False),
        patch.object(_e2e.time, "sleep"),
        patch.object(_e2e.time, "monotonic", side_effect=[100.0, 137.25]),
    ):
        assert _e2e.robust_destroy(DSEQ, owner=OWNER) is False
    output = capsys.readouterr().out
    assert "settlement not observed after 37.2 s" in output
    assert "within 24 s" not in output


def test_audit_receives_the_captured_owner():
    with (
        patch.object(_e2e, "_run", return_value=_completed(0, "Deployment destroyed")) as run,
        patch.object(_e2e, "_confirm_settled", return_value=True) as confirm,
        patch.object(_e2e.time, "sleep"),
    ):
        assert _e2e.robust_destroy(DSEQ, owner=OWNER) is True
    assert run.call_args.args[0] == f"just destroy {DSEQ}"
    confirm.assert_called_once_with(DSEQ, OWNER)


def _compiled_destroy(source: str):
    namespace = {
        "is_canonical_akash_address": _e2e.is_canonical_akash_address,
        "_confirm_settled": lambda _dseq, _owner: True,
        "_confirm_settled_single_reader": lambda _dseq: False,
        "_destroy_succeeded": lambda _result: True,
        "_fail": lambda _message: None,
        "_pass": lambda _message: None,
        "_run": lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="Deployment destroyed", stderr=""
        ),
        "shlex": __import__("shlex"),
        "time": SimpleNamespace(sleep=lambda _seconds: None, monotonic=lambda: 0.0),
    }
    exec(source, namespace)
    return namespace["robust_destroy"]


def test_removing_owner_scoped_audit_call_is_a_red_effect_mutation():
    source = textwrap.dedent(inspect.getsource(_e2e.robust_destroy))
    target = "_confirm_settled(dseq, owner)"
    replacement = "_confirm_settled_single_reader(dseq)"
    assert source.count(target) == 1, "call-site mutation target must apply exactly once"
    mutant = source.replace(target, replacement, 1)
    assert mutant != source

    assert _compiled_destroy(source)(DSEQ, owner=OWNER) is True
    assert _compiled_destroy(mutant)(DSEQ, owner=OWNER) is False


def _unowned_e2e_wiring(source_by_path: dict[str, str]) -> list[str]:
    marker = "destroy_owned_deployment as robust_destroy"
    return [path for path, source in source_by_path.items() if marker not in source]


def test_every_real_e2e_cleanup_is_wired_through_owner_capture():
    sources = {path: (ROOT / path).read_text() for path in E2E_CALLERS}
    assert len(sources) == 4
    assert _unowned_e2e_wiring(sources) == []


def test_removing_one_e2e_owner_capture_call_site_is_a_red_mutation():
    sources = {path: (ROOT / path).read_text() for path in E2E_CALLERS}
    path = E2E_CALLERS[0]
    target = "destroy_owned_deployment as robust_destroy"
    assert sources[path].count(target) == 1, "call-site mutation must apply exactly once"
    sources[path] = sources[path].replace(target, "robust_destroy", 1)
    assert _unowned_e2e_wiring(sources) == [path]
