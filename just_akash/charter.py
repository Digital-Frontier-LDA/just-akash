"""Lease charters: no Akash lease of OURS without a declared purpose, owner, budget and expiry.

WHY THIS EXISTS (openmix-pvr6, implemented here as openmix-pvr6.1 / just-akash-3sq)
-------------------------------------------------------------------------------
Flynn attributes every lease on our wallets by its SDL placement name (openmix
app/background/leases.py): ``flynn-*``, ``fleet-*`` and the exact names ``dcloud`` / ``akash``
are OURS. Before charters, such a lease could be created with nothing on record saying what
it was for or when it should stop, and was funded with a flat ``--deposit`` plus auto
top-up — so nothing expired it. A charter fixes all three at the point of spend:

  * it is POSTed to Flynn BEFORE the first spend, and the deploy refuses if that fails;
  * its id is stamped into the placement name (provenance.stamp_charter) and bound to the
    final dseq AFTER the lease exists;
  * the escrow is sized from ``max_hours`` at the accepted bid's price and auto top-up is
    switched OFF, so the lease runs out when the charter says it should.

ROLLOUT: ``JUST_AKASH_CHARTER=warn`` (default) emits CHARTER_MISSING and proceeds;
``enforce`` refuses. Placements Flynn does not count as ours (``dfci-*``, ``just-akash-*``)
are untouched in both modes.

⚠ The validation mirrors Flynn's ``validate_charter`` (openmix 8943c379,
app/background/lease_charters.py) so a bad charter fails HERE, before any network call,
rather than as a 422 after the wallet was already picked. Flynn remains the authority.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .provenance import CHARTER_ID_RE, is_ours_placement
from .wallet_pool import SECONDS_PER_BLOCK

__all__ = [
    "CHARTER_ID_RE",
    "Charter",
    "CharterError",
    "CharterInvalid",
    "CharterMissing",
    "FlynnCharterClient",
    "deposit_for",
    "gate",
    "is_ours_placement",
    "load_charter",
    "mode",
    "wallet_label_for",
]

MODE_ENV = "JUST_AKASH_CHARTER"
CHARTER_JSON_ENV = "JUST_AKASH_CHARTER_JSON"
WALLET_LABELS_ENV = "JUST_AKASH_WALLET_LABELS"
FLYNN_URL_ENV = "FLYNN_API_URL"
FLYNN_KEY_ENV = "FLYNN_INTERNAL_API_KEY"
MODES = ("warn", "enforce")

# The Console's minimum escrow deposit (USD/ACT). A chartered order is created at exactly
# this and topped up once the bid's price is known.
MIN_DEPOSIT = 0.5
BLOCKS_PER_HOUR = 3600.0 / SECONDS_PER_BLOCK
# Headroom over the bid price for block-time drift; a lease that dies a few minutes early
# reads as a provider fault, so the error has to land on the over side.
DEPOSIT_MARGIN = 1.15

# Flynn's limits (lease_charters.py): kept equal so a charter valid here is valid there.
KINDS = ("job", "capacity")
MAX_HOURS_CAP = 24.0 * 90
_CAPACITY_FIELDS = ("served_model_name", "image_digest", "canary_prompt", "canary_expect")
_WALLET_LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_URL_RE = re.compile(r"https?://[^\s/]+(/[^\s]*)?")

BIND_ATTEMPTS = 3
BIND_RETRY_DELAY_S = 2.0
_TIMEOUT_S = 15


class CharterMissing(RuntimeError):
    """A placement of ours was about to be deployed without a charter, under enforce."""


class CharterInvalid(RuntimeError):
    """A charter document that cannot say what the lease is for, until when, or for how much.
    Names EVERY problem: fixing one field per attempt against a deploy gate wastes a run each."""

    def __init__(self, problems: list[str]):
        super().__init__("invalid lease charter: " + "; ".join(problems))
        self.problems = problems


class CharterError(RuntimeError):
    """Flynn did not accept the charter (or cannot be reached safely). Refuses the deploy."""


def _text(doc: dict, key: str, limit: int) -> str | None:
    value = doc.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


@dataclass(frozen=True)
class Charter:
    bead: str
    owner: str
    why: str
    kind: str
    max_hours: float
    max_act: float
    prod_pinned: bool = False
    served_model_name: str | None = None
    image_digest: str | None = None
    canary_prompt: str | None = None
    canary_expect: str | None = None
    startup_timeout_s: int | None = None
    idle_threshold_pct: float | None = None
    # Local-only: these feed the BIND, never the charter POST.
    wallet_label: str | None = None
    endpoint_url: str | None = None
    telemetry_url: str | None = None

    _LOCAL = ("wallet_label", "endpoint_url", "telemetry_url")

    @classmethod
    def from_dict(cls, doc: Any) -> Charter:
        if not isinstance(doc, dict):
            raise CharterInvalid(["charter must be a JSON object"])
        problems: list[str] = []
        out: dict[str, Any] = {}
        for key, limit in (("bead", 64), ("owner", 64), ("why", 512)):
            out[key] = _text(doc, key, limit)
            if not out[key]:
                problems.append(f"{key} is required")
        kind = str(doc.get("kind") or "").strip()
        if kind not in KINDS:
            problems.append("kind must be exactly 'job' or 'capacity'")
        out["kind"] = kind
        for key, cap in (("max_hours", MAX_HOURS_CAP), ("max_act", None)):
            try:
                value = float(doc.get(key))  # type: ignore[arg-type]
                if not (value > 0) or not math.isfinite(value) or (cap and value > cap):
                    raise ValueError
                out[key] = value
            except (TypeError, ValueError):
                problems.append(
                    f"{key} must be a number > 0" + (f" and <= {cap:g}" if cap else "")
                )
        # Stricter than Flynn, and only here: the order itself costs MIN_DEPOSIT, so a budget
        # below it could never be honoured.
        if isinstance(out.get("max_act"), float) and out["max_act"] < MIN_DEPOSIT:
            problems.append(f"max_act must be >= {MIN_DEPOSIT} (the Console's minimum deposit)")
        if doc.get("startup_timeout_s") is not None:
            try:
                out["startup_timeout_s"] = int(doc["startup_timeout_s"])
                if out["startup_timeout_s"] <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append("startup_timeout_s must be a positive integer")
        if doc.get("idle_threshold_pct") is not None:
            try:
                out["idle_threshold_pct"] = float(doc["idle_threshold_pct"])
                if not 0 <= out["idle_threshold_pct"] <= 100:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append("idle_threshold_pct must be 0..100")
        # A JSON boolean only: bool("false") is True, and prod_pinned switches auto top-up ON.
        pinned = doc.get("prod_pinned")
        if pinned is not None and not isinstance(pinned, bool):
            problems.append("prod_pinned must be a JSON boolean")
        out["prod_pinned"] = pinned is True
        for key, limit in zip(_CAPACITY_FIELDS, (256, 128, 1024, 256), strict=True):
            out[key] = _text(doc, key, limit)
        if kind == "capacity":
            for key in _CAPACITY_FIELDS:
                if not out[key]:
                    problems.append(f"{key} is required for kind=capacity")
            if out["image_digest"] and not re.fullmatch(
                r"sha256:[0-9a-f]{64}", out["image_digest"]
            ):
                problems.append("image_digest must be sha256:<64 hex>")
        if out["canary_expect"]:
            try:
                re.compile(out["canary_expect"])
            except re.error:
                problems.append("canary_expect is not a valid regex")
        if out["prod_pinned"] and kind != "capacity":
            problems.append("prod_pinned is only valid for kind=capacity")

        out["wallet_label"] = _text(doc, "wallet_label", 64)
        if out["wallet_label"] and not _WALLET_LABEL_RE.fullmatch(out["wallet_label"]):
            problems.append("wallet_label must match [A-Za-z0-9_.-]{1,64}")
        for key in ("endpoint_url", "telemetry_url"):
            value = _text(doc, key, 512)
            if value and not _URL_RE.fullmatch(value):
                problems.append(f"{key} is not an http(s) URL")
            out[key] = value
        if problems:
            raise CharterInvalid(problems)
        return cls(**out)

    def flynn_payload(self) -> dict[str, Any]:
        """The charter as Flynn's POST takes it: local-only fields and unset optionals dropped."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name not in self._LOCAL and getattr(self, f.name) is not None
        }


def load_charter(path: str | None) -> Charter | None:
    """The charter from ``--charter FILE`` or, failing that, ``JUST_AKASH_CHARTER_JSON``.
    None when neither is given; CharterInvalid when one is given and wrong."""
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise CharterInvalid([f"cannot read charter file {path}: {e}"]) from e
    else:
        raw = os.environ.get(CHARTER_JSON_ENV, "").strip()
        if not raw:
            return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CharterInvalid([f"charter is not valid JSON: {e}"]) from e
    return Charter.from_dict(doc)


def mode() -> str:
    """``warn`` (default) or ``enforce``. Anything else raises: a typo such as ``enforced``
    must not silently read as warn and let uncharted leases out."""
    value = os.environ.get(MODE_ENV, "").strip().lower() or "warn"
    if value not in MODES:
        raise RuntimeError(f"{MODE_ENV}={value!r} is not one of {', '.join(MODES)}")
    return value


def gate(placement_keys: list[str], charter: Charter | None, gate_mode: str) -> list[str]:
    """The placements of ours that this deploy would create WITHOUT a charter.

    Raises CharterMissing under enforce when there are any; under warn the caller reports
    them and proceeds. Empty for every caller Flynn does not count as ours, in both modes.
    """
    uncharted = [] if charter else [k for k in placement_keys if is_ours_placement(k)]
    if uncharted and gate_mode == "enforce":
        raise CharterMissing(
            f"refusing to deploy {', '.join(uncharted)} without a lease charter "
            f"({MODE_ENV}=enforce). Pass --charter FILE.json or {CHARTER_JSON_ENV}."
        )
    return uncharted


def deposit_for(max_hours: float, price_uact_per_block: float, max_act: float) -> float:
    """Escrow (USD/ACT) to fund ``max_hours`` at the accepted bid: hours x uact/block x
    blocks/h x margin, capped at the charter budget and floored at the Console minimum."""
    price = float(price_uact_per_block)
    if not math.isfinite(price) or price < 0:
        raise ValueError(f"cannot size a deposit from bid price {price_uact_per_block!r}")
    raw = float(max_hours) * price * BLOCKS_PER_HOUR * DEPOSIT_MARGIN / 1_000_000
    return max(MIN_DEPOSIT, min(raw, float(max_act)))


def wallet_label_for(charter: Charter, address: str | None, configured_keys: int = 1) -> str:
    """The wallet LABEL Flynn binds by (``console-2``) for the wallet the pool SELECTED.
    just-akash only knows addresses, so the label comes from the charter's own
    ``wallet_label`` or ``JUST_AKASH_WALLET_LABELS="akash1…=label,…"`` — and must describe
    the wallet that actually pays. Resolved before the spend: a lease bound to the wrong
    wallet (or not at all) is an uncharted lease.

    ⛔ WITH SEVERAL KEYS THE CHARTER'S LABEL IS ONLY A CLAIM. The pool picks the wallet, so
    a static ``wallet_label`` names the right one only by luck. It is accepted when the
    address map confirms it for the selected address, or when there is just one key; a
    contradiction, or several keys with nothing to confirm it, refuses."""
    mapping: dict[str, str] = {}
    for piece in re.split(r"[\n,;]", os.environ.get(WALLET_LABELS_ENV, "")):
        addr, sep, label = piece.strip().partition("=")
        if sep and addr.strip() and label.strip():
            mapping[addr.strip()] = label.strip()
    mapped = mapping.get(address or "")
    if charter.wallet_label:
        if mapped and mapped != charter.wallet_label:
            raise RuntimeError(
                f"the charter's wallet_label {charter.wallet_label!r} is not the selected "
                f"wallet, which {WALLET_LABELS_ENV} labels {mapped!r}"
            )
        if not mapped and configured_keys > 1:
            raise RuntimeError(
                f"the charter's wallet_label {charter.wallet_label!r} cannot be confirmed for "
                f"the selected wallet: {configured_keys} keys are configured, so map their "
                f"addresses in {WALLET_LABELS_ENV}"
            )
        return charter.wallet_label
    if not mapped or not _WALLET_LABEL_RE.fullmatch(mapped):
        raise RuntimeError(
            "no wallet label for the charter bind: set wallet_label in the charter or map "
            f"this wallet's address in {WALLET_LABELS_ENV}"
        )
    return mapped


class FlynnCharterClient:
    """Flynn's internal lease-charter API (openmix-pvr6): POST /api/v1/leases/charters and
    POST /api/v1/leases/charters/{id}/bind, authenticated with an INTERNAL bearer key."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.environ.get(FLYNN_URL_ENV, "")).strip().rstrip("/")
        self.api_key = (api_key or os.environ.get(FLYNN_KEY_ENV, "")).strip()
        if not self.base_url or not self.api_key:
            raise CharterError(
                f"a lease charter needs {FLYNN_URL_ENV} and {FLYNN_KEY_ENV} to reach Flynn"
            )
        parsed = urlparse(self.base_url)
        # The key is a bearer header: plaintext http only to this machine.
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise CharterError(f"{FLYNN_URL_ENV} must be https (got {parsed.scheme or 'none'}://)")

    def _post(self, path: str, body: dict) -> tuple[int, Any]:
        req = urllib.request.Request(  # noqa: S310 — scheme checked in __init__
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return status, payload

    def post(self, charter: Charter) -> str:
        """Write the charter; its ``charter_id``. Anything but 201 + a well-formed id raises."""
        try:
            status, payload = self._post("/api/v1/leases/charters", charter.flynn_payload())
        except (OSError, urllib.error.URLError) as e:
            raise CharterError(f"Flynn charter POST failed: {e}") from e
        if status != 201:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise CharterError(f"Flynn refused the charter (HTTP {status}): {detail!r}")
        charter_id = payload.get("charter_id") if isinstance(payload, dict) else None
        if not isinstance(charter_id, str) or not CHARTER_ID_RE.fullmatch(charter_id):
            raise CharterError(f"Flynn returned a malformed charter_id: {charter_id!r}")
        return charter_id

    def bind(
        self,
        charter_id: str,
        *,
        wallet: str,
        dseq: str,
        endpoint_url: str | None = None,
        telemetry_url: str | None = None,
    ) -> dict:
        """Bind the charter to the deployment it paid for. Idempotent on Flynn's side for the
        same (wallet, dseq), so a retry after a lost response is safe. 5xx and transport
        errors are retried; a 4xx is an answer and is not."""
        body = {
            "wallet": wallet,
            "dseq": str(dseq),
            "endpoint_url": endpoint_url,
            "telemetry_url": telemetry_url,
        }
        last = ""
        for attempt in range(1, BIND_ATTEMPTS + 1):
            try:
                status, payload = self._post(f"/api/v1/leases/charters/{charter_id}/bind", body)
            except (OSError, urllib.error.URLError) as e:
                status, payload, last = 0, None, str(e)
            if status == 200:
                return payload if isinstance(payload, dict) else {}
            if status:
                last = f"HTTP {status}: {payload!r}"
            if 400 <= status < 500 or attempt == BIND_ATTEMPTS:
                break
            time.sleep(BIND_RETRY_DELAY_S)
        raise CharterError(f"Flynn charter bind failed for {charter_id}: {last}")
