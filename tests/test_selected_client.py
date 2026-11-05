"""Only the first sender object changes; original captured source/guards remain."""

import pytest
from test_hosted_cli import PROVIDER, api, hosted, invoke_deploy
from test_hosted_cli import scene as scene


@pytest.mark.parametrize("other_client", [False, True])
def test_first_sender_must_be_actual_captured_client(scene, monkeypatch, other_client):
    s = scene
    original_capture = hosted.capture_prepared_lease
    observed = {}

    def capture_then_probe(client, receipt, dseq, sdl, manifest):
        original_capture(client, receipt, dseq, sdl, manifest)
        sender = api.AkashConsoleAPI(api_key=client.api_key) if other_client else client
        observed["same_object"] = sender is client
        try:
            sender.create_lease(str(dseq), PROVIDER, manifest, 1, 1)
        except BaseException as error:
            observed["failure_type"] = type(error).__name__
        observed["lease_posts"] = sum(m == "POST" and p == "/v1/leases" for m, p, b in s.console)
        observed["broker_phases"] = tuple(s.broker.phases)
        # Stop original deployment after probe; historical cleanup may run but
        # never creates another lease or proves shared-store settlement.
        raise RuntimeError("CLOSED_PROBE_COMPLETE")

    monkeypatch.setattr(hosted, "capture_prepared_lease", capture_then_probe)
    with hosted.install_root_hosted_cli(s.registration()):
        invoke_deploy(s, monkeypatch)
    assert observed["same_object"] is (not other_client)
    if other_client:
        assert observed["lease_posts"] == 0, observed
        assert observed["broker_phases"] == (), observed
    else:
        assert observed["lease_posts"] == 1, observed
        assert observed["broker_phases"] == ("reserve", "consume", "outcome"), observed
