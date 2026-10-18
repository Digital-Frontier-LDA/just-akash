"""Shared harness for the lease-charter tests (just-akash-3sq / openmix-pvr6.1).

Drives the REAL deploy() over real urllib HTTP against two localhost fakes: the Console
stub in _fake_akash.py (extended with deposit-deployment and deployment-settings) and
FakeFlynn. Both append to ONE event list, and the wallet-credit probe is replaced by a
recorder on the same list, so a test can read the order of every spend-relevant call.

No chain, no Flynn, no credential: every base URL is 127.0.0.1 (test_charter_flynn_order
asserts that, so a refactor that quietly pointed a fake at a real host fails loudly).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from just_akash.api import AkashConsoleAPI
from tests._fake_akash import FakeConsole, FakeFlynn

# Captured at import (collection), before any test runs. Another module's test leaves a
# MagicMock on `just_akash.deploy.AkashConsoleAPI` (just-akash-o69), and a
# deploy() that silently talks to a mock would make every assertion here vacuous.
REAL_CONSOLE_API = AkashConsoleAPI


def sdl_text(placement_key: str) -> str:
    return f"""version: '2.0'
services:
  web:
    image: nginx:1.25
    expose:
      - port: 80
        to:
          - global: true
profiles:
  compute:
    web:
      resources:
        cpu:
          units: 0.5
        memory:
          size: 512Mi
        storage:
          size: 1Gi
  placement:
    {placement_key}:
      pricing:
        web:
          denom: uact
          amount: 1000
deployment:
  web:
    {placement_key}:
      profile: web
      count: 1
"""


def charter_doc(**overrides) -> dict:
    doc = {
        "bead": "openmix-pvr6.1",
        "owner": "jonathan",
        "why": "charter-gate test lease",
        "kind": "job",
        "max_hours": 10,
        "max_act": 50,
        "wallet_label": "console-2",
        "endpoint_url": "https://serve.example/v1",
        "telemetry_url": "https://serve.example/metrics",
    }
    doc.update(overrides)
    return doc


@dataclass
class Harness:
    console: FakeConsole
    flynn: FakeFlynn
    tmp_path: Path
    events: list = field(default_factory=list)

    @property
    def app(self):
        return self.console.app

    def write_sdl(self, placement_key: str) -> str:
        p = self.tmp_path / f"{placement_key}.yaml"
        p.write_text(sdl_text(placement_key))
        return str(p)

    def write_charter(self, **overrides) -> str:
        p = self.tmp_path / "charter.json"
        p.write_text(json.dumps(charter_doc(**overrides)))
        return str(p)

    def set_bid_price(self, amount, denom: str = "uact") -> None:
        for b in self.app.bids:
            b["price"] = {"amount": amount, "denom": denom}

    def names(self) -> list[str]:
        return [e[0] for e in self.events]


class _FastTime:
    """deploy's `time`, with every sleep capped: its bid poll waits a hard-coded 5 s, which
    is 5 s of nothing per test against a fake that answers instantly."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(min(seconds, 0.05))


def start(monkeypatch, tmp_path) -> Harness:
    from just_akash import deploy as deploy_mod

    console = FakeConsole()
    events: list = []
    console.app.events = events
    flynn = FakeFlynn(events)
    h = Harness(console=console, flynn=flynn, tmp_path=tmp_path, events=events)

    monkeypatch.setattr(deploy_mod, "AkashConsoleAPI", REAL_CONSOLE_API)
    monkeypatch.setattr("just_akash.api.AkashConsoleAPI", REAL_CONSOLE_API)
    monkeypatch.setattr(deploy_mod, "time", _FastTime())
    monkeypatch.setattr(
        deploy_mod,
        "_check_wallet_credit",
        lambda client, deposit: events.append(("credit_probe", deposit)),
    )
    for name in (
        "AKASH_PROVIDERS",
        "AKASH_PROVIDERS_BACKUP",
        "JUST_AKASH_CHARTER",
        "JUST_AKASH_CHARTER_JSON",
        "JUST_AKASH_WALLET_LABELS",
        "AKASH_API_KEYS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_CONSOLE_URL", console.base_url)
    monkeypatch.setenv("FLYNN_API_URL", flynn.base_url)
    monkeypatch.setenv("FLYNN_INTERNAL_API_KEY", "fake-internal-key")
    # The issue-#19 re-deploy round's fast-poll: no courtesy wait, tight interval.
    monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "0")
    monkeypatch.setenv("JUST_AKASH_REDEPLOY_POLL_INTERVAL_S", "0.1")
    monkeypatch.setenv("JUST_AKASH_REDEPLOY_WAIT_S", "5")
    monkeypatch.setenv("AKASH_DIAGNOSTICS", "1")
    return h


def stop(h: Harness) -> None:
    h.flynn.stop()
    h.console.stop()
