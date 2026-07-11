"""`exec` must be able to target a service explicitly.

Why this exists: LeaseShellTransport inferred the target container from
`lease.status.services`. The Akash Console API populates that field LAZILY, so it
can still be empty after a container is demonstrably running -- our CI runner was
registered and online with GitHub while the lease still reported no services. Every
`exec` then died with "Cannot determine service name", which a caller could easily
misread as a broken node rather than "not reported yet".

Two fixes are pinned here:
  1. An explicit service_name bypasses inference entirely.
  2. The two failure modes -- nothing reported yet vs. several to choose from --
     no longer share one message.
"""

import contextlib
import logging

import pytest

from just_akash.transport.base import TransportConfig
from just_akash.transport.lease_shell import LeaseShellTransport


def _cfg(services, service_name=None):
    return TransportConfig(
        dseq="123",
        api_key="k",
        service_name=service_name,
        deployment={
            "leases": [
                {
                    "id": {"provider": "akash1prov"},
                    "provider": {"hostUri": "https://provider.example:8443"},
                    "status": {"services": services},
                }
            ]
        },
    )


def test_explicit_service_bypasses_inference_when_lease_reports_none():
    """The load-bearing case: lease reports NO services (lazy API), but we know the name."""
    t = LeaseShellTransport(_cfg(services={}, service_name="runner"))
    host, service = t._extract_provider_info()
    assert service == "runner"
    assert host == "https://provider.example:8443"


def test_explicit_service_wins_over_inference():
    t = LeaseShellTransport(_cfg(services={"api": {}, "runner": {}}, service_name="runner"))
    _, service = t._extract_provider_info()
    assert service == "runner"


def test_no_services_reported_says_not_ready_and_points_at_the_flag():
    t = LeaseShellTransport(_cfg(services={}))
    with pytest.raises(RuntimeError) as e:
        t._extract_provider_info()
    msg = str(e.value)
    assert "has not reported any service" in msg
    assert "--service" in msg  # tells the caller how to proceed
    assert "LAZILY" in msg  # names the real trap


def test_multiple_services_warns_and_still_falls_back_to_the_first(caplog):
    """Inference picks the FIRST reported service -- an arbitrary choice on a
    multi-service deployment. That stays (removing it would break every existing
    single-service caller) but it must not be silent: warn, name them, and point at
    --service. Blazing's deployment has six services; "whichever is first" is a
    footgun, not a feature."""
    tr = LeaseShellTransport(_cfg(services={"api": {}, "runner": {}, "redis": {}}))
    with caplog.at_level(logging.WARNING, logger="just_akash.transport.lease_shell"):
        _, service = tr._extract_provider_info()

    assert service in {"api", "runner", "redis"}  # behaviour preserved
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert "3 services" in warning
    assert "api, redis, runner" in warning  # names them
    assert "--service" in warning  # points at the escape hatch


def test_known_services_reports_every_service_the_lease_lists():
    tr = LeaseShellTransport(_cfg(services={"api": {}, "runner": {}, "redis": {}}))
    assert sorted(tr._known_services()) == ["api", "redis", "runner"]


class TestServiceFlagReachesTheTransport:
    """The flag is worthless if it is parsed but never passed down.

    The whole point of this change is that TransportConfig.service_name existed but
    nothing on the CLI could reach it, so the tool's own advice ("pass service_name")
    was impossible to follow. Pin the wiring, for both subcommands.
    """

    @pytest.mark.parametrize("subcommand", ["exec", "connect"])
    def test_service_flag_is_passed_into_make_transport(self, subcommand, monkeypatch):
        import sys

        import just_akash.cli as cli

        captured = {}

        class _FakeTransport:
            def validate(self):
                return True

            def prepare(self):
                return None

            def exec(self, _cmd):
                return 0

            def connect(self):
                return 0

        def _fake_make_transport(_name, **kwargs):
            captured.update(kwargs)
            return _FakeTransport()

        monkeypatch.setattr("just_akash.transport.make_transport", _fake_make_transport)
        monkeypatch.setattr(cli, "_require_api_key", lambda: "key")
        monkeypatch.setattr(cli, "_resolve_deployment", lambda _c, d: d or "123")
        monkeypatch.setattr(cli, "_enrich_deployment_with_provider", lambda _c, d: d)

        class _FakeClient:
            api_key = "key"  # pragma: allowlist secret  (test dummy, not a credential)

            def get_deployment(self, _dseq):
                return {"leases": [{}]}

        monkeypatch.setattr("just_akash.api.AkashConsoleAPI", lambda _k: _FakeClient())

        argv = ["just-akash", subcommand, "--dseq", "123", "--service", "runner"]
        if subcommand == "exec":
            argv.append("echo hi")
        monkeypatch.setattr(sys, "argv", argv)

        # exec exits with the remote return code; connect may simply return.
        with contextlib.suppress(SystemExit):
            cli.main()

        assert captured.get("service_name") == "runner", (
            f"--service never reached make_transport for `{subcommand}`; got {captured!r}"
        )
