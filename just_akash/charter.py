"""Lease charter gate for OUR placement names (openmix-pvr6 AC1/AC2/AC3).

A lease charter declares, BEFORE the spend, what a lease is for, who owns it, how long it may
live and how much it may cost. Flynn stores it (POST /api/v1/leases/charters -> charter_id),
binds it to the deployment it paid for (POST /api/v1/leases/charters/{id}/bind) and pages or
closes any lease of ours that has none.

SCOPE — only OUR placement names (provenance.is_ours: `flynn-*`, `fleet-*`, exactly `dcloud` or
`akash`). Every other caller of this tool (`dfci-*`, `just-akash-*`, anything else) shares the
wallets and is UNCHANGED in every mode: no charter is read, posted, stamped or required.

MODE — env JUST_AKASH_CHARTER:
  warn (default)  a missing or failing charter is LOGGED and the deploy goes on (the rollout
                  state: Flynn's reconciler pages an uncharted lease of ours, so a miss is seen).
  enforce         a missing, invalid or unrecorded charter REFUSES the deploy before any
                  wallet check or create_deployment call: no spend without a declared purpose.
  off             the gate does nothing.

JOB charters also bound the lease ON CHAIN: the order is created with the Console's
`runtimeLimitHours` = ceil(max_hours) (1..48) and the limit is READ BACK before any bid is
accepted; one that does not read back exactly closes the order unleased (just-akash #448).
The Console then closes the deployment itself at the limit. A deposit cannot do this: the
Console ignores the create deposit and funds every deployment itself (console #3783).

CREDENTIALS — the Flynn internal key comes from FLYNN_CHARTER_KEY, else FLYNN_CANARY_KEY, read
from the environment at call time. It is sent only as a request header to FLYNN_API_URL (https
only) and never logged, printed or put in argv.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .provenance import CHARTER_ID_RE, ours_keys

logger = logging.getLogger("akash.charter")

MODE_ENV = "JUST_AKASH_CHARTER"
MODES = ("warn", "enforce", "off")
DEFAULT_MODE = "warn"

DEFAULT_FLYNN_API_URL = "https://api.flynn.digitalfrontier.so"
CHARTERS_PATH = "/api/v1/leases/charters"
HTTP_TIMEOUT_S = 20

KIND_JOB = "job"
KIND_CAPACITY = "capacity"
KINDS = (KIND_JOB, KIND_CAPACITY)
#: Flynn's lease_charters.MAX_HOURS_CAP: a charter is a budget, not "forever".
MAX_HOURS_CAP = 24.0 * 90
#: The Console grants at most 48 h of runtime limit at create (POST /v1/deployments schema).
MAX_RUNTIME_LIMIT_HOURS = 48
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: owner address -> wallet LABEL, the same public table as Flynn's lease_owner_wallets and
#: scripts/akash/lib_charter.sh. The bind must carry the label the lease snapshot uses.
OWNER_LABELS: Mapping[str, str] = {
    "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee": "console-2",
    "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt": "console-3",
}


class CharterRefused(RuntimeError):
    """The gate said no. Raised only in enforce mode, and always before any spend."""


class CharterMissing(CharterRefused):
    """An OURS placement and no charter, in enforce mode."""


class CharterInvalid(ValueError):
    """A charter that cannot say what the lease is for, until when, or for how much."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class CharterAPIError(RuntimeError):
    """Flynn did not record (or bind) the charter. The message never carries the key."""


def resolve_mode(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    raw = (env.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    if raw not in MODES:
        # A typo must not quietly read as warn (or off): refuse, it costs nothing yet.
        raise CharterRefused(f"{MODE_ENV}={raw!r} is not one of {'/'.join(MODES)}")
    return raw


def load_charter(arg: str | Mapping[str, Any]) -> dict[str, Any]:
    """A charter from a JSON object, a JSON string, or a path to a JSON file."""
    if isinstance(arg, Mapping):
        return dict(arg)
    text = str(arg).strip()
    if not text.startswith("{"):
        path = Path(text)
        if not path.is_file():
            raise CharterInvalid([f"--charter {text!r} is neither a JSON object nor a file"])
        text = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise CharterInvalid([f"charter is not valid JSON ({e.msg})"]) from None
    if not isinstance(doc, dict):
        raise CharterInvalid(["charter must be a JSON object"])
    return doc


def _text(doc: Mapping[str, Any], k: str) -> str:
    v = doc.get(k)
    return "" if v is None else str(v).strip()


def _number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def validate_charter(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The charter as it will be POSTed, or CharterInvalid naming EVERY problem.

    Mirrors Flynn's validate_charter, so a charter Flynn would refuse is refused here, before
    the gate spends a round trip (or, in warn mode, a lease) on it.
    """
    if not isinstance(doc, Mapping):
        raise CharterInvalid(["charter must be a JSON object"])
    problems: list[str] = []
    out = dict(doc)
    for k in ("bead", "owner", "why"):
        out[k] = _text(doc, k)
        if not out[k]:
            problems.append(f"{k} is required")
    kind = _text(doc, "kind")
    out["kind"] = kind
    if kind not in KINDS:
        problems.append("kind must be exactly 'job' or 'capacity'")
    for k in ("max_hours", "max_act"):
        v = _number(doc.get(k))
        if v is None or (k == "max_hours" and v > MAX_HOURS_CAP):
            problems.append(
                f"{k} must be a finite number > 0"
                + (f" and <= {MAX_HOURS_CAP:g}" if k == "max_hours" else "")
            )
        else:
            out[k] = v
    if kind == KIND_CAPACITY:
        for k in ("served_model_name", "image_digest", "canary_prompt", "canary_expect"):
            if not _text(doc, k):
                problems.append(f"{k} is required for kind=capacity")
        digest = _text(doc, "image_digest")
        if digest and not _DIGEST_RE.match(digest):
            problems.append("image_digest must be sha256:<64 hex>")
    expect = _text(doc, "canary_expect")
    if expect:
        try:
            re.compile(expect)
        except re.error:
            problems.append("canary_expect is not a valid regex")
    if doc.get("prod_pinned") and kind != KIND_CAPACITY:
        problems.append("prod_pinned is only valid for kind=capacity")
    if problems:
        raise CharterInvalid(problems)
    return out


def runtime_limit_for(charter: Mapping[str, Any]) -> int | None:
    """runtimeLimitHours for a JOB charter: ceil(max_hours), at least 1. None for capacity.

    May exceed MAX_RUNTIME_LIMIT_HOURS; the gate decides what to do with that (see gate()).
    """
    if charter.get("kind") != KIND_JOB:
        return None
    return max(1, math.ceil(float(charter["max_hours"])))


def owner_label(address: str | None) -> str | None:
    return OWNER_LABELS.get(address or "")


# ── Flynn API ────────────────────────────────────────────────────────────────────────────


def _api_key(env: Mapping[str, str]) -> str:
    return (env.get("FLYNN_CHARTER_KEY") or env.get("FLYNN_CANARY_KEY") or "").strip()


def _base_url(env: Mapping[str, str]) -> str:
    url = (env.get("FLYNN_API_URL") or DEFAULT_FLYNN_API_URL).strip().rstrip("/")
    parts = urlsplit(url)
    # https only: the request carries a bearer key.
    if parts.scheme != "https" or not parts.netloc:
        raise CharterAPIError("FLYNN_API_URL must be an https:// URL")
    return url


def _detail(body: bytes) -> str:
    """A bounded one-line reason from Flynn's error body (its `detail`), never raw bytes."""
    try:
        doc = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    d = doc.get("detail") if isinstance(doc, dict) else None
    if isinstance(d, (list, tuple)):
        d = "; ".join(str(x) for x in d)
    return " ".join(str(d or "").split())[:300]


Opener = Callable[[urllib.request.Request, float], Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse EVERY redirect. `_base_url` checks only the first URL, and urllib follows a POST's
    301/302/303 as a GET carrying the original headers, Authorization included: one redirect
    would hand the Flynn bearer key to any host, over plain http too. The charter API never
    redirects, so a 3xx is an error (HTTPError -> CharterAPIError in _post), never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    # https enforced by _base_url; redirects refused by _NoRedirect
    return _OPENER.open(req, timeout=timeout)


def _post(
    path: str,
    body: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    opener: Opener | None = None,
) -> tuple[int, dict[str, Any]]:
    key = _api_key(env)
    if not key:
        raise CharterAPIError("no FLYNN_CHARTER_KEY / FLYNN_CANARY_KEY in the environment")
    req = urllib.request.Request(  # noqa: S310 - https enforced by _base_url
        f"{_base_url(env)}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "just-akash-charter/1.0",
        },
        method="POST",
    )
    try:
        with (opener or _default_opener)(req, HTTP_TIMEOUT_S) as resp:
            status = int(getattr(resp, "status", 0) or resp.getcode())
            raw = resp.read()
    except urllib.error.HTTPError as e:
        reason = _detail(e.read() or b"")
        raise CharterAPIError(
            f"POST {path} -> HTTP {e.code}" + (f": {reason}" if reason else "")
        ) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise CharterAPIError(f"POST {path} failed ({type(e).__name__})") from None
    try:
        doc = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        doc = {}
    return status, doc if isinstance(doc, dict) else {}


def create_remote(
    charter: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> str:
    """POST the charter; its charter_id (ch-<10 hex>), or CharterAPIError."""
    env = os.environ if env is None else env
    status, doc = _post(CHARTERS_PATH, charter, env=env, opener=opener)
    if status != 201:
        raise CharterAPIError(f"POST {CHARTERS_PATH} -> HTTP {status}, expected 201")
    cid = str(doc.get("charter_id") or "")
    if not CHARTER_ID_RE.match(cid):
        raise CharterAPIError("charter API answered 201 without a valid charter_id")
    return cid


def bind_remote(
    charter_id: str,
    *,
    wallet: str,
    dseq: str,
    endpoint_url: str | None = None,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> None:
    env = os.environ if env is None else env
    if not CHARTER_ID_RE.match(charter_id or ""):
        raise CharterAPIError("not a charter id")
    body: dict[str, Any] = {"wallet": wallet, "dseq": str(dseq)}
    if endpoint_url:
        body["endpoint_url"] = endpoint_url
    path = f"{CHARTERS_PATH}/{charter_id}/bind"
    status, _ = _post(path, body, env=env, opener=opener)
    if status != 200:
        raise CharterAPIError(f"POST {path} -> HTTP {status}, expected 200")


# ── The gate ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CharterPlan:
    """What the gate decided. `ours` False means: this deploy is not ours, do nothing."""

    mode: str = "off"
    ours: bool = False
    keys: list[str] = field(default_factory=list)
    charter: dict[str, Any] | None = None
    charter_id: str | None = None
    runtime_limit_hours: int | None = None

    @property
    def enforce(self) -> bool:
        return self.ours and self.mode == "enforce"

    def fail(self, msg: str, exc: type[CharterRefused] = CharterRefused) -> None:
        """enforce -> raise; warn -> log and go on."""
        if self.enforce:
            logger.error(f"[charter] REFUSED (enforce): {msg}")
            raise exc(f"charter gate (enforce): {msg}")
        logger.warning(f"[charter] WARN (mode={self.mode}, deploy continues): {msg}")


def gate(
    sdl_text: str,
    charter_arg: str | Mapping[str, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
    opener: Opener | None = None,
) -> CharterPlan:
    """Decide the charter for one deploy. Runs BEFORE any wallet check or create.

    Not ours -> an inert plan, whatever the mode or the charter argument (other callers are
    unchanged). Ours -> per mode: validate locally, then record the charter at Flynn.
    """
    env = os.environ if env is None else env
    keys = ours_keys(sdl_text)
    if not keys:
        if charter_arg is not None:
            logger.info("[charter] --charter ignored: no flynn-/fleet-/dcloud/akash placement")
        return CharterPlan()
    plan = CharterPlan(mode=resolve_mode(env), ours=True, keys=keys)
    if plan.mode == "off":
        return plan
    if charter_arg is None:
        plan.fail(f"placement {keys} is ours and no --charter was given", CharterMissing)
        return plan
    try:
        charter = validate_charter(load_charter(charter_arg))
    except CharterInvalid as e:
        plan.fail(f"invalid charter: {e}")
        return plan
    plan.charter = charter
    hours = runtime_limit_for(charter)
    if hours is not None and hours > MAX_RUNTIME_LIMIT_HOURS:
        # The Console cannot express more than 48 h at create. Shortening the job silently
        # would kill work its charter allows; not bounding it is what this gate prevents.
        plan.fail(
            f"job charter max_hours={charter['max_hours']:g} exceeds the Console's "
            f"{MAX_RUNTIME_LIMIT_HOURS} h runtime limit at create; created WITHOUT a runtime "
            "limit (Flynn's charter reconciler is the only bound)"
        )
        hours = None
    plan.runtime_limit_hours = hours
    try:
        plan.charter_id = create_remote(charter, env=env, opener=opener)
    except CharterAPIError as e:
        plan.fail(f"charter not recorded at Flynn: {e}")
        return plan
    logger.info(
        f"[charter] {plan.charter_id} ({charter['kind']}, {charter['max_hours']:g} h, "
        f"{charter['max_act']:g} ACT) for {charter['bead']}"
    )
    return plan


def verify_runtime_limit(
    client: Any,
    dseq: str,
    hours: int,
    *,
    tries: int = 4,
    poll_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Read runtimeLimitHours back from GET /v2/deployment-settings/{dseq}; raise unless it IS
    `hours` (an int, not a bool). Never trust the write: this API has accepted fields it then
    ignored. A settings row that is not there yet ({}) is polled a few times."""
    got: object = None
    why = ""
    for i in range(tries):
        try:
            settings = client.get_deployment_settings(str(dseq))
        except Exception as e:  # noqa: BLE001 - any doubt about the limit is a refusal
            why = f" ({type(e).__name__})"
            break
        if settings:
            got = settings.get("runtimeLimitHours") if isinstance(settings, dict) else None
            why = ""
            break
        why = " (no settings row)"
        if i + 1 < tries:
            sleep(poll_s)
    if isinstance(got, bool) or not isinstance(got, int) or got != hours:
        shown = repr(got) if got is None or isinstance(got, (bool, int)) else type(got).__name__
        raise RuntimeError(
            f"runtime limit reads back {shown}{why}, asked {hours} h; not leasing an order "
            "that could outlive its charter"
        )
