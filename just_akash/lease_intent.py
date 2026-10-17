"""Record WHY a just-akash lease exists in Flynn's lease intent ledger (Flynn openmix-h2l7).

Flynn's /leases page and its `flynn_lease_intent{dseq}` gauge show, for every active lease on
our Console wallets, whether something declared why it exists: who opened it, for what, how
long it may run and how much it may spend. just-akash's persistent canaries were the leases
with no such declaration (prod 2026-10-17: 1789100636167, 1789100716448, 1789100804519, all
console-2). This module is the just-akash writer:

  create   POST /api/v1/lease-intents         before a canary is deployed (prints the id)
  bind     POST /api/v1/lease-intents/{id}/bind   once the deploy printed its dseq
  abandon  POST /api/v1/lease-intents/{id}/close  the deploy did not happen (-> abandoned)
  close    POST /api/v1/lease-intents/by-dseq/{dseq}/close   the deployment was closed
  backfill   register + bind leases that already run (the three canaries above)

CREDENTIAL. Keycloak machine client `just-akash-lease-intent` (Akash-Console #1147), realm
console-staging, client_credentials, scope flynn-lease-intent. The secret is
JUST_AKASH_LEASE_INTENT_CLIENT_SECRET, delivered to CI by .github/actions/sops-env from
secrets/ci.sops.env. It and the access token travel only in request BODIES and headers: never
argv, never a file, never a log line.

NO SECRET = INERT AND SILENT. Until the secret exists every command does nothing and exits 0,
so wiring this into a workflow changes nothing before the credential lands.

MODE (JUST_AKASH_INTENT_MODE): `warn` (default) logs a failure and exits 0, so the canary is
never blocked by the ledger; `enforce` exits 1 on failure; `off` does nothing.

BOTH LIMITS RUN FROM THE LEASE'S CREATION ON CHAIN, not from the intent's: Flynn flags
INTENT_EXCEEDED when `age_h > max_hours` or `act_per_day * age_h / 24 > max_spend_act`, with
age measured from the lease's created height. For a NEW lease that is the same thing. A
BACKFILL must therefore pass the lease's age plus the horizon, never a bare horizon: four
720 h intents on 760-870 h old Flynn leases were red the moment they were bound (pvr6.6,
Flynn #1504/#1506). `backfill` computes max_hours from chain heights for that reason.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

CLIENT_ID = "just-akash-lease-intent"
SCOPE = "flynn-lease-intent"
REPO = "Digital-Frontier-LDA/just-akash"
DEFAULT_TOKEN_URL = (
    "https://auth.console.digitalfrontier.so/realms/console-staging/protocol/openid-connect/token"  # noqa: S105 - a URL, not a password
)
DEFAULT_FLYNN_URL = "https://api.flynn.digitalfrontier.so"
DEFAULT_CHAIN_URL = "https://api.akashnet.net"
SECRET_ENV = "JUST_AKASH_LEASE_INTENT_CLIENT_SECRET"  # noqa: S105  # pragma: allowlist secret - a NAME
MODE_ENV = "JUST_AKASH_INTENT_MODE"
TIMEOUT_S = 15
UA = "just-akash-lease-intent/1.0"

# Owner ADDRESS -> the wallet label Flynn accepts (Flynn app/settings lease_owner_wallets).
# Public addresses of our three Console wallets; a deploy from any other account is never
# recorded under a guessed label.
WALLET_LABELS = {
    "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0": "console-1",  # pragma: allowlist secret
    "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee": "console-2",  # pragma: allowlist secret
    "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt": "console-3",  # pragma: allowlist secret
}

#: Seconds per block, the figure Flynn's lease report uses (app/background/leases.py).
BLOCK_TIME_S = 5.883
BLOCKS_PER_DAY = 86400.0 / BLOCK_TIME_S
#: Default horizon a persistent canary declares beyond its current age.
DEFAULT_HORIZON_H = 720.0
#: Flynn's ceiling on max_hours (app/api/v1/lease_intents.py MAX_HOURS).
FLYNN_MAX_HOURS = 2160.0
#: Spend cap headroom over price x hours, so a price move within the auction does not trip it.
SPEND_HEADROOM = 1.25

_DSEQ = re.compile(r"^[1-9][0-9]{0,19}$")
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class IntentError(RuntimeError):
    """A ledger call failed. The message never contains the secret or the token."""


def _log(msg: str) -> None:
    print(f"[intent] {msg}", file=sys.stderr)


def _https(url: str) -> str:
    """Refuse anything but https before a request is built: an env override of a URL must not
    turn into a file:// read or a cleartext POST of the client secret."""
    if not url.startswith("https://"):
        raise IntentError("refusing a non-https URL")
    return url


# The transport is injectable so tests never touch the network.
Transport = Callable[[urllib.request.Request], tuple[int, bytes]]


def _urlopen(req: urllib.request.Request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - https-only, guarded by _https
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # network failure: say what kind, never the payload
        raise IntentError(f"request failed ({type(e).__name__})") from None


class Ledger:
    def __init__(
        self,
        secret: str,
        *,
        transport: Transport = _urlopen,
        token_url: str | None = None,
        flynn_url: str | None = None,
    ) -> None:
        self._secret = secret
        self._transport = transport
        self._token_url = _https(
            token_url or os.environ.get("JUST_AKASH_INTENT_TOKEN_URL") or DEFAULT_TOKEN_URL
        )
        self._flynn = _https(
            (
                flynn_url or os.environ.get("JUST_AKASH_INTENT_FLYNN_URL") or DEFAULT_FLYNN_URL
            ).rstrip("/")
        )
        self._token: str | None = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "scope": SCOPE,
                "client_secret": self._secret,
            }
        ).encode()
        req = urllib.request.Request(  # noqa: S310 - scheme checked by _https()
            self._token_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
        )
        status, raw = self._transport(req)
        if status != 200:
            raise IntentError(f"token request refused (HTTP {status})")
        try:
            tok = json.loads(raw or b"{}").get("access_token") or ""
        except ValueError:
            tok = ""
        if not tok:
            raise IntentError("token response carried no access_token")
        self._token = tok
        return tok

    def _post(self, path: str, doc: dict) -> tuple[int, dict]:
        req = urllib.request.Request(  # noqa: S310 - scheme checked by _https()
            _https(self._flynn + path),
            data=json.dumps(doc).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": UA,
                "Authorization": f"Bearer {self._get_token()}",
            },
        )
        status, raw = self._transport(req)
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            out = {}
        return status, out if isinstance(out, dict) else {}

    def create(self, doc: dict) -> str:
        status, out = self._post("/api/v1/lease-intents", doc)
        iid = str(out.get("id") or "")
        if status != 201 or not _ID.fullmatch(iid):
            raise IntentError(f"register refused (HTTP {status})")
        return iid

    def bind(self, iid: str, dseq: str) -> None:
        if not _ID.fullmatch(iid) or not _DSEQ.fullmatch(dseq):
            raise IntentError("bind needs an intent id and a canonical dseq")
        status, _ = self._post(f"/api/v1/lease-intents/{iid}/bind", {"dseq": dseq})
        if status != 200:
            raise IntentError(f"bind {dseq} refused (HTTP {status})")

    def abandon(self, iid: str, reason: str) -> None:
        if not _ID.fullmatch(iid):
            raise IntentError("abandon needs an intent id")
        status, _ = self._post(
            f"/api/v1/lease-intents/{iid}/close", {"reason": (reason or "not deployed")[:500]}
        )
        if status != 200:
            raise IntentError(f"abandon refused (HTTP {status})")

    def close(self, dseq: str, reason: str) -> bool:
        """True when an intent was closed; False when none is bound to dseq (404 is normal)."""
        if not _DSEQ.fullmatch(dseq):
            raise IntentError("close needs a canonical dseq")
        status, _ = self._post(
            f"/api/v1/lease-intents/by-dseq/{dseq}/close", {"reason": (reason or "closed")[:500]}
        )
        if status == 404:
            return False
        if status != 200:
            raise IntentError(f"close {dseq} refused (HTTP {status})")
        return True


def intent_doc(
    *,
    reason: str,
    wallet: str,
    max_hours: float,
    group_name: str | None = None,
    max_spend_act: float | None = None,
    workflow: str | None = None,
    run_url: str | None = None,
    actor: str | None = None,
    bead: str | None = None,
) -> dict:
    """The register body. Only contract keys; Flynn refuses unknown ones."""
    if wallet not in WALLET_LABELS.values():
        raise IntentError("wallet is not one of our Console wallets")
    if not 0 < max_hours <= FLYNN_MAX_HOURS:
        raise IntentError(f"max_hours must be in (0, {FLYNN_MAX_HOURS:g}]")
    doc: dict = {
        "repo": REPO,
        "reason": reason[:1000],
        "charter": "CAPACITY",
        "wallet": wallet,
        "max_hours": float(max_hours),
    }
    for key, val, limit in (
        ("workflow", workflow, 200),
        ("run_url", run_url, 300),
        ("actor", actor, 64),
        ("bead", bead, 64),
        ("group_name", group_name, 128),
    ):
        if val:
            doc[key] = val[:limit]
    if max_spend_act is not None:
        if max_spend_act <= 0:
            raise IntentError("max_spend_act must be positive")
        doc["max_spend_act"] = float(max_spend_act)
    return doc


def ci_context() -> dict:
    """workflow / run_url / actor from the GitHub Actions environment, when present."""
    env = os.environ
    out = {
        "workflow": env.get("GITHUB_WORKFLOW") or None,
        "actor": env.get("GITHUB_ACTOR") or None,
    }
    if env.get("GITHUB_SERVER_URL") and env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID"):
        out["run_url"] = (
            f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}"
            f"/actions/runs/{env['GITHUB_RUN_ID']}"
        )
    return out


def act_per_day(price_uact_per_block: float) -> float:
    return price_uact_per_block * BLOCKS_PER_DAY / 1e6


def backfill_limits(
    *,
    created_height: int,
    latest_height: int,
    price_uact_per_block: float,
    horizon_h: float = DEFAULT_HORIZON_H,
) -> tuple[float, float]:
    """(max_hours, max_spend_act) for a lease that already runs: its chain age plus the
    horizon, and the spend that rate implies over those hours with SPEND_HEADROOM. Rounded
    UP, so the cap is never below what the lease has already used."""
    if latest_height < created_height:
        raise IntentError("latest height is below the lease's created height")
    age_h = (latest_height - created_height) * BLOCK_TIME_S / 3600.0
    max_hours = math.ceil(age_h + horizon_h)
    if max_hours > FLYNN_MAX_HOURS:
        raise IntentError(f"age {age_h:.0f} h + horizon exceeds Flynn's {FLYNN_MAX_HOURS:g} h cap")
    spend = act_per_day(price_uact_per_block) * max_hours / 24.0 * SPEND_HEADROOM
    return float(max_hours), max(math.ceil(spend * 100) / 100.0, 0.01)


# ── chain reads for backfill (public REST, no credential) ──────────────────────────────────


def _chain_get(path: str, transport: Transport, chain_url: str) -> dict:
    req = urllib.request.Request(  # noqa: S310 - scheme checked by _https()
        _https(chain_url.rstrip("/") + path),
        headers={"Accept": "application/json", "User-Agent": UA},
    )
    status, raw = transport(req)
    if status != 200:
        raise IntentError(f"chain read refused (HTTP {status})")
    try:
        return json.loads(raw)
    except ValueError:
        raise IntentError("chain read was not JSON") from None


def read_lease(
    owner: str, dseq: str, *, transport: Transport = _urlopen, chain_url: str = DEFAULT_CHAIN_URL
) -> dict:
    """{created_height, latest_height, price, group_name, state} for owner's lease dseq."""
    q = urllib.parse.urlencode({"filters.owner": owner, "filters.dseq": dseq})
    leases = (
        _chain_get(f"/akash/market/v1beta5/leases/list?{q}", transport, chain_url).get("leases")
        or []
    )
    active = [
        entry["lease"] for entry in leases if entry.get("lease", {}).get("state") == "active"
    ]
    if len(active) != 1:
        raise IntentError(f"{dseq}: expected one active lease, found {len(active)}")
    lease = active[0]
    q = urllib.parse.urlencode({"id.owner": owner, "id.dseq": dseq})
    dep = _chain_get(f"/akash/deployment/v1beta4/deployments/info?{q}", transport, chain_url)
    gseq = int(lease["id"].get("gseq", 1))
    names = [
        (g.get("group_spec") or {}).get("name") or ""
        for g in dep.get("groups") or []
        if int((g.get("id") or {}).get("gseq", -1)) == gseq
    ]
    latest = _chain_get("/cosmos/base/tendermint/v1beta1/blocks/latest", transport, chain_url)
    return {
        "created_height": int(lease["created_at"]),
        "latest_height": int(latest["block"]["header"]["height"]),
        "price": float((lease.get("price") or {}).get("amount") or 0.0),
        "group_name": names[0] if names else "",
        "state": lease.get("state"),
    }


def close_quietly(dseq: str, reason: str, *, transport: Transport = _urlopen) -> None:
    """Close the intent bound to `dseq`, if there is one. For callers whose own job is
    closing deployments (close_orphans): inert without the secret, never raises, and a
    failure is one log line, so the ledger can never stand in the way of a close."""
    if os.environ.get(MODE_ENV, "warn") == "off" or not os.environ.get(SECRET_ENV):
        return
    try:
        if Ledger(os.environ[SECRET_ENV], transport=transport).close(dseq, reason):
            _log(f"intent for {dseq} closed")
    except Exception as e:  # noqa: BLE001 - the close it follows already happened
        _log(f"WARN: could not close the intent for {dseq} ({type(e).__name__})")


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────


def _ledger_or_none(transport: Transport) -> Ledger | None:
    mode = os.environ.get(MODE_ENV, "warn")
    if mode == "off":
        return None
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        return None
    return Ledger(secret, transport=transport)


def _fail(msg: str) -> int:
    if os.environ.get(MODE_ENV, "warn") == "enforce":
        _log(f"REFUSED (enforce): {msg}")
        return 1
    _log(f"WARN (the canary goes on): {msg}")
    return 0


def main(argv: list[str] | None = None, *, transport: Transport = _urlopen) -> int:
    p = argparse.ArgumentParser(
        prog="python -m just_akash.lease_intent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="register an intent before a deploy; prints its id")
    c.add_argument("--owner", required=True, help="the deploying wallet's owner address")
    c.add_argument("--group-name", required=True)
    c.add_argument("--reason", required=True)
    c.add_argument("--max-hours", type=float, default=DEFAULT_HORIZON_H)
    c.add_argument("--max-spend-act", type=float)
    c.add_argument("--bead")
    b = sub.add_parser("bind", help="attach the deployed dseq")
    b.add_argument("--id", required=True)
    b.add_argument("--dseq", required=True)
    a = sub.add_parser("abandon", help="the deploy did not happen")
    a.add_argument("--id", required=True)
    a.add_argument("--reason", default="deploy did not happen")
    x = sub.add_parser("close", help="the deployment was closed")
    x.add_argument("--dseq", required=True)
    x.add_argument("--reason", default="deployment closed")
    f = sub.add_parser("backfill", help="register + bind leases that already run")
    f.add_argument("--owner", required=True)
    f.add_argument("--dseq", required=True, nargs="+")
    f.add_argument("--reason", required=True)
    f.add_argument("--horizon-hours", type=float, default=DEFAULT_HORIZON_H)
    f.add_argument(
        "--group-prefix",
        default="just-akash-canary",
        help="refuse a lease whose group name does not start with this",
    )
    f.add_argument("--bead")
    args = p.parse_args(argv)

    try:
        ledger = _ledger_or_none(transport)
    except IntentError as e:  # e.g. a non-https URL override: refused before any request
        return _fail(str(e))
    if ledger is None:
        return 1 if (args.cmd == "create" and os.environ.get(MODE_ENV) == "enforce") else 0

    try:
        if args.cmd == "create":
            wallet = WALLET_LABELS.get(args.owner)
            if not wallet:
                return _fail("the deploying account is not one of our Console wallets")
            doc = intent_doc(
                reason=args.reason,
                wallet=wallet,
                max_hours=args.max_hours,
                group_name=args.group_name,
                max_spend_act=args.max_spend_act,
                bead=args.bead,
                **ci_context(),
            )
            iid = ledger.create(doc)
            _log(f"intent {iid} (CAPACITY, {args.max_hours:g} h) on {wallet}")
            print(iid)
        elif args.cmd == "bind":
            ledger.bind(args.id, args.dseq)
            _log(f"intent {args.id} bound to {args.dseq}")
        elif args.cmd == "abandon":
            ledger.abandon(args.id, args.reason)
            _log(f"intent {args.id} abandoned")
        elif args.cmd == "close":
            if ledger.close(args.dseq, args.reason):
                _log(f"intent for {args.dseq} closed")
        elif args.cmd == "backfill":
            wallet = WALLET_LABELS.get(args.owner)
            if not wallet:
                return _fail("the owner is not one of our Console wallets")
            bad = [d for d in args.dseq if not _DSEQ.fullmatch(d)]
            if bad:
                return _fail(f"not a dseq: {bad}")
            rc = 0
            for dseq in args.dseq:
                try:
                    lease = read_lease(args.owner, dseq, transport=transport)
                    if not lease["group_name"].startswith(args.group_prefix):
                        raise IntentError(
                            f"{dseq}: group {lease['group_name']!r} is not a "
                            f"{args.group_prefix} lease"
                        )
                    hours, spend = backfill_limits(
                        created_height=lease["created_height"],
                        latest_height=lease["latest_height"],
                        price_uact_per_block=lease["price"],
                        horizon_h=args.horizon_hours,
                    )
                    doc = intent_doc(
                        reason=args.reason,
                        wallet=wallet,
                        max_hours=hours,
                        group_name=lease["group_name"],
                        max_spend_act=spend,
                        bead=args.bead,
                        **ci_context(),
                    )
                    iid = ledger.create(doc)
                    try:
                        ledger.bind(iid, dseq)
                    except IntentError:
                        # never leave a requested intent behind for a lease it does not name
                        ledger.abandon(iid, f"backfill bind of {dseq} failed")
                        raise
                    _log(f"{dseq}: intent {iid} bound ({hours:g} h, {spend:g} ACT)")
                except IntentError as e:
                    rc = max(rc, _fail(f"{dseq}: {e}"))
            return rc
    except IntentError as e:
        return _fail(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
