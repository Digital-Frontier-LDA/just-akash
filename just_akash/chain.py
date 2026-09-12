"""Read-only Akash chain queries over a Cosmos REST (LCD) endpoint.

The Console API this tool normally talks to exposes NO balance endpoint (see
``smoke_providers`` — the only credit signal it has is a 402 on deploy). But the
credit *is* on-chain: Console holds the real funds in a managed depositor wallet
and grants each account an escrow ``DepositAuthorization`` whose ``spend_limits``
is the remaining deploy credit. That grant, and the account's liquid bank balance,
are both plain public-chain state, so we read them straight from a public LCD with
stdlib HTTP — no ``akash`` binary, no secret, nothing spent.

``AKASH_REST_URL`` overrides the endpoint; the default is the same provider that
backs the default ``AKASH_NODE`` RPC.
"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

# Companion to the default AKASH_NODE (akash-rpc.publicnode.com): the same provider's
# REST/LCD host. A public default matches how AKASH_NODE already defaults.
DEFAULT_REST_URL = "https://akash-rest.publicnode.com"

# Additional public LCDs, queried alongside the default when reading deploy credit.
#
# WHY: a single public LCD can lag, and a lagging node UNDER-reports a grant because
# it has not yet seen the newest deposit. Measured 2026-08-06 on one account:
#
#     api.akashnet.net        407.85 ACT   (expiration 2036-08-04)
#     akash-api.polkachu.com  407.85 ACT   (expiration 2036-08-04)
#     akash-rest.publicnode.com  246.19 ACT   (expiration 2036-07-14)  <- default
#
# The default was $161 behind and still serving an expired-and-replaced grant. Any
# caller gating on credit — `balance --check --min-usd`, the Prometheus credit gauge,
# a CI preflight — would report a funded account as short and take the failure path.
# In CI that means falling back to paid runners while the wallet is fine.
#
# Reconciled by LATEST EXPIRATION (see `deploy_credit`). The OLD rule — MAX across
# endpoints, justified as "staleness can only lose a deposit, never invent one" — is
# FALSE: a lagging node can serve a SUPERSEDED grant whose remaining allowance is
# LARGER than the replacement's. Measured 2026-08-29 on akash1n4uut3…: publicnode
# served 170.62 ACT (expiration 2036-07-08) while akashnet + polkachu agreed on the
# fresh 116.33 ACT vintage (2036-08-24). MAX picked the dead grant, and every
# deposit sized between the two was refused with 402 for 13 days.
DEFAULT_REST_FALLBACKS = (
    "https://api.akashnet.net",
    "https://akash-api.polkachu.com",
)

# Destructive identity reads use trust paths, not the availability/credit endpoints above.
# Source labels and URLs: cosmos/chain-registry, akash/chain.json; observed and DNS
# ancestry checked 2026-09-12. Bump the registry version when any entry changes.
OWNER_CORROBORATION_SOURCES_V1 = (
    {
        "source_id": "quad",
        "url": "https://akash.rpc.uquad.org:443",
        "chain_id": "akashnet-2",
        "operator": "quad",
        "gateway_ancestry": "direct-88.198.50.175",
        "cache_ancestry": "none-direct-88.198.50.175",
        "proof_mode": "height-pinned-block-id-and-paginated-group-list",
        "finality_rule": "committed-tip-minus-2",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    },
    {
        "source_id": "c29r3",
        "url": "https://akash.c29r3.xyz:443/api",
        "chain_id": "akashnet-2",
        "operator": "c29r3",
        "gateway_ancestry": "direct-65.21.234.82",
        "cache_ancestry": "none-direct-65.21.234.82",
        "proof_mode": "height-pinned-block-id-and-paginated-group-list",
        "finality_rule": "committed-tip-minus-2",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    },
)
# Pocket is deliberately absent. It returned deployment data during the 2026-09-12
# capability probe but did not echo ``x-cosmos-block-height``. That makes the response
# useful for availability reads and ineligible for height-pinned destructive authority.


def _source_registry_digest(sources) -> str:
    return hashlib.sha256(
        json.dumps(
            sources,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


OWNER_CORROBORATION_REGISTRY_VERSION = 1
OWNER_CORROBORATION_REGISTRY_PROVENANCE = (
    "https://github.com/cosmos/chain-registry/blob/"
    "c67c94a5f5c41ad1b116b1b847ef8b5f196b6405/akash/chain.json"
)
OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256 = (
    "071d561eccb4a26ac4e3404b58ce1d1fb2b881c230e4468f78b44d92b8c5cd51"
)
OWNER_CORROBORATION_REGISTRY_SHA256 = _source_registry_digest(OWNER_CORROBORATION_SOURCES_V1)

# Akash's own escrow authorization type (custom, not a generic cosmos SendAuthorization).
_DEPOSIT_AUTH_TYPE = "/akash.escrow.v1.DepositAuthorization"

# Human labels for the denoms we expect. Both are 6-decimal ("micro") units.
# uact = Akash Credit Token, the USD-pegged Console credit; uakt = AKT.
_DENOM_META = {
    "uact": {"label": "ACT", "decimals": 6, "usd_pegged": True},
    "uakt": {"label": "AKT", "decimals": 6, "usd_pegged": False},
}


class ChainResponseError(RuntimeError):
    """A chain endpoint answered, but its response cannot support the requested read."""


def rest_url() -> str:
    """The LCD base URL (no trailing slash), from env or the public default.

    Restricted to http/https so a crafted ``AKASH_REST_URL`` (e.g. ``file://``)
    can't point ``urllib`` at a local resource — this is what justifies the
    ``# noqa: S310`` on the ``urlopen`` calls below. Raises RuntimeError on any
    other scheme.
    """
    url = os.environ.get("AKASH_REST_URL", DEFAULT_REST_URL).rstrip("/")
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(
            f"AKASH_REST_URL must use an http/https scheme; got {scheme!r} from {url!r}"
        )
    return url


def rest_urls() -> list[str]:
    """Every LCD to consult, most-trusted first.

    An explicit ``AKASH_REST_URL`` is an operator decision and is honoured ALONE —
    silently querying other hosts would defeat the point of pinning one (an
    air-gapped or private LCD, a node under test). Only the default path fans out.
    """
    # `is not None`, NOT truthiness. An explicitly-set-but-empty AKASH_REST_URL is a
    # misconfiguration, and treating it as "not pinned" silently fans out to the public
    # defaults — the exact opposite of what someone pinning an air-gapped or private LCD
    # asked for. Defer to rest_url(), which raises on an empty value, so the two agree.
    pinned = os.environ.get("AKASH_REST_URL")
    if pinned is not None:
        return [rest_url()]
    return [DEFAULT_REST_URL, *DEFAULT_REST_FALLBACKS]


def _lcd_get(
    path: str, timeout: int = 15, base: str | None = None, height: int | None = None
) -> dict[str, Any]:
    """GET a Cosmos REST path and return parsed JSON. Raises RuntimeError on any
    transport/HTTP/parse failure, with the endpoint in the message so a dead LCD is
    obvious (and swappable via AKASH_REST_URL)."""
    url = f"{(base or rest_url()).rstrip('/')}{path}"
    headers = {"Accept": "application/json", "User-Agent": "just-akash-balance/1.0"}
    if height is not None:
        headers["x-cosmos-block-height"] = str(height)
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — fixed base
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw_body = resp.read()
            echoed = getattr(resp, "headers", {}).get("x-cosmos-block-height")
    except urllib.error.HTTPError as e:
        raise ChainResponseError(f"chain query returned HTTP {e.code} ({url})") from e
    except Exception as e:  # noqa: BLE001 — normalize every failure to one error type
        raise RuntimeError(f"chain query failed ({url}): {type(e).__name__}: {e}") from e
    try:
        body = raw_body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ChainResponseError(f"chain query returned non-UTF-8 ({url})") from e
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise ChainResponseError(f"chain query returned non-JSON ({url}): {body[:200]}") from e
    if not isinstance(parsed, dict):
        raise ChainResponseError(f"chain query returned unexpected shape ({url}): {type(parsed)}")
    if height is not None:
        try:
            if echoed is None or int(echoed) != height:
                raise ChainResponseError(f"chain query did not echo pinned height ({url})")
        except (TypeError, ValueError) as e:
            raise ChainResponseError(f"chain query returned invalid pinned height ({url})") from e
    return parsed


# The akash module's CURRENT REST version. v1beta3 is gone: every configured endpoint
# answers it with HTTP 501 "Not Implemented" while serving v1beta4 with a 200. That 501
# is what this repo recorded as "public LCD nodes don't serve akash-module queries" — it
# was a version mismatch, not a limitation of public nodes, and the difference matters:
# one closes the door on reading chain-native deployment state, the other is a URL edit.
#
# Verified 2026-08-12 against all three configured endpoints (publicnode, akashnet,
# polkachu): v1beta3 -> 501, v1beta4 -> 200.
_DEPLOYMENT_API = "/akash/deployment/v1beta4"

# ⚠ THE MARKET MODULE IS ON A DIFFERENT VERSION FROM DEPLOYMENT, AND THAT IS EASY TO GET
# BACKWARDS. Deployments answer on v1beta4 (above); market/leases answer on v1beta5 and
# return 501 on v1beta4. Verified 2026-08-25 against the configured endpoints.
_MARKET_API = "/akash/market/v1beta5"


def active_deployment_count(owner: str, timeout: int = 15) -> int | None:
    """How many ACTIVE deployments the chain attributes to ``owner``.

    ⛔ Returns ``None`` — never 0 — when the chain cannot be read. This exists to
    CORROBORATE a Console listing, so collapsing "could not ask" into "zero" would
    defeat its only purpose: it would confirm an empty listing with an empty answer.
    """
    path = (
        f"{_DEPLOYMENT_API}/deployments/list"
        f"?filters.owner={owner}&filters.state=active&pagination.limit=1000"
    )
    try:
        data = _lcd_get(path, timeout=timeout)
    except RuntimeError:
        return None
    deployments = data.get("deployments")
    if not isinstance(deployments, list):
        return None
    return len(deployments)


def list_active_deployments(owner: str, timeout: int = 15) -> list[dict[str, Any]] | None:
    """Every ACTIVE deployment record the chain attributes to ``owner``, or None if unknown.

    ⛔ WHY THIS EXISTS: THE CONSOLE LISTING CANNOT SCOPE TO AN ACCOUNT. `list_deployments`
    sends `GET /v1/deployments` and relies on the API key to scope the response server-side.
    It does not. MEASURED 2026-08-30 from one host, three DISTINCT keys for three DISTINCT
    accounts, same minute:

        AKASH_CONSOLE    -> n=2  sha256(body)[:10] = 56432a8d66
        AKASH_CONSOLE_2  -> n=2  sha256(body)[:10] = 56432a8d66
        AKASH_CONSOLE_3  -> n=2  sha256(body)[:10] = 56432a8d66

    Byte-identical bodies for three different accounts, against a chain showing 23 / 42 / 0
    active. The same endpoint is separately NON-DETERMINISTIC over time — 44 / 27 / 0 for one
    key minutes apart, every time HTTP 200 — which is what `verify_not_silently_empty` was
    written to catch downstream. Both faults have the same fix: ask the chain, which is
    keyless, per-owner and authoritative.

    ⛔ None IS NOT []. `[]` means "asked, and this owner genuinely holds nothing"; ``None``
    means "could not ask". Collapsing them is how a sweeper skips a wallet with no error for
    an unknown number of cycles, and a caller that DESTROYS things must branch on the
    difference. Every failure path below returns None — never a partial page.

    ⚠ PAGINATED, unlike :func:`active_deployment_count`, which asks for `limit=1000` once and
    would silently truncate a larger account. Truncation here is not a smaller report, it is
    a set of deployments that are invisible to the sweep.
    """
    if not owner:
        return None
    base_path = (
        f"{_DEPLOYMENT_API}/deployments/list"
        f"?filters.owner={urllib.parse.quote(owner)}&filters.state=active&pagination.limit=200"
    )
    # One endpoint answers for the whole listing. Paging ACROSS endpoints could interleave
    # two nodes at different heights and produce a set that never existed at any height.
    for base in rest_urls():
        out: list[dict[str, Any]] = []
        next_key: str | None = None
        ok = True
        for _ in range(50):  # hard page cap — a runaway cursor must not loop forever
            path = base_path
            if next_key:
                path += f"&pagination.key={urllib.parse.quote(next_key)}"
            try:
                data = _lcd_get(path, timeout=timeout, base=base)
            except RuntimeError:
                ok = False
                break  # try the next endpoint; one lagging node must not answer for the chain
            deployments = data.get("deployments")
            if not isinstance(deployments, list):
                ok = False
                break
            if any(not isinstance(d, dict) for d in deployments):
                ok = False
                break  # a malformed sibling is an incomplete population, never an empty one
            out.extend(deployments)
            # ⛔ A MALFORMED CURSOR IS "UNKNOWN", NOT "DONE". Treating an unreadable
            # `pagination` as end-of-list returns a PARTIAL set that looks complete — the
            # same empty-vs-failed collapse this function refuses one level up, and the
            # caller's next act is to close what it did not see. A non-string `next_key`
            # would additionally raise TypeError inside `quote`, which is a crash rather
            # than a verdict.
            pagination = data.get("pagination")
            if pagination is not None and not isinstance(pagination, dict):
                ok = False
                break
            raw = pagination.get("next_key") if isinstance(pagination, dict) else None
            if raw is None or raw == "":
                return out
            if not isinstance(raw, str):
                ok = False
                break
            next_key = raw
        else:
            ok = False  # exhausted the page cap without terminating — refuse the partial
        if ok:
            return out
    return None


def deployment_group_names(owner: str, dseq: str) -> list[str]:
    """``group_spec.name`` for every group of one deployment, read from chain.

    This is the READ half of just_akash.provenance. The placement key an SDL declares
    becomes ``group_spec.name`` inside ``MsgCreateDeployment`` — author-controlled,
    written atomically, immutable afterwards — so reading it back is how a deployment
    proves WHO created it. Nothing else on chain does: just-akash's tags live in a local
    file, and the Console API exposes no tag at all.

    Without this, ownership could only be inferred from shape (service names, age), which
    is why `cleanup_stale --reap-runners` had to be an operator's assertion rather than a
    check, and why a suspected orphan could only be reported and never acted on. A sweep
    that reaps on shape alone once destroyed 14 third-party deployments.

    Returns [] when the deployment cannot be read — from every endpoint, or because it no
    longer exists. An empty list therefore means UNKNOWN, never "not ours", and a caller
    that destroys things must treat it as such.
    """
    if not owner or not dseq:
        return []
    path = (
        f"{_DEPLOYMENT_API}/deployments/info"
        f"?id.owner={urllib.parse.quote(owner)}&id.dseq={urllib.parse.quote(dseq)}"
    )
    for base in rest_urls():
        try:
            data = _lcd_get(path, base=base)
        except RuntimeError:
            continue  # one dead or lagging endpoint must not answer for the whole chain
        deployment = data.get("deployment")
        deployment_id = deployment.get("id") if isinstance(deployment, dict) else None
        if not isinstance(deployment_id, dict):
            continue
        if deployment_id.get("owner") != owner or str(deployment_id.get("dseq")) != dseq:
            continue
        groups = data.get("groups")
        if not isinstance(groups, list) or not groups:
            continue
        # ALL-OR-NOTHING per response. A partial parse — three groups, two readable —
        # would claim ownership from incomplete evidence, and the caller uses this to
        # decide whether to DESTROY. Half an answer is not a weaker proof, it is a
        # different deployment's proof. So an unnamed group makes the whole response
        # unreadable and we try the next endpoint, which may simply be healthier.
        names: list[str] = []
        for g in groups:
            group_id = g.get("id") if isinstance(g, dict) else None
            name = (g.get("group_spec") or {}).get("name") if isinstance(g, dict) else None
            if (
                not isinstance(group_id, dict)
                or group_id.get("owner") != owner
                or str(group_id.get("dseq")) != dseq
                or not isinstance(name, str)
                or not name
            ):
                names = []
                break
            names.append(name)
        if names:
            return names
    return []


def _deployment_group_snapshot(
    data: dict[str, Any], owner: str, dseq: str
) -> tuple[tuple[str, str], ...] | None:
    """Parse one complete deployment-group population, including every group id."""

    # ``deployments/info`` is an atomic, non-paginated endpoint. It has no total-count
    # contract to reconcile. If a server starts returning pagination metadata, refuse it
    # instead of silently treating the first page as the complete group population.
    if "pagination" in data:
        return None
    deployment = data.get("deployment")
    deployment_id = deployment.get("id") if isinstance(deployment, dict) else None
    if not isinstance(deployment_id, dict):
        return None
    if deployment_id.get("owner") != owner or str(deployment_id.get("dseq")) != dseq:
        return None

    return _group_rows_snapshot(data.get("groups"), owner, dseq)


def _group_rows_snapshot(
    groups: object, owner: str, dseq: str
) -> tuple[tuple[str, str], ...] | None:
    """Parse a complete set of group rows without inferring that the set is complete."""
    if not isinstance(groups, list) or not groups:
        return None
    snapshot: dict[str, str] = {}
    for group in groups:
        group_id = group.get("id") if isinstance(group, dict) else None
        spec = group.get("group_spec") if isinstance(group, dict) else None
        if not isinstance(group_id, dict) or not isinstance(spec, dict):
            return None
        gseq = group_id.get("gseq")
        if isinstance(gseq, bool) or not isinstance(gseq, (str, int)):
            return None
        gseq_text = str(gseq)
        name = spec.get("name")
        if (
            group_id.get("owner") != owner
            or str(group_id.get("dseq")) != dseq
            or not re.fullmatch(r"[1-9][0-9]{0,31}", gseq_text)
            or str(int(gseq_text)) in snapshot
            or not isinstance(name, str)
            or not name
        ):
            return None
        snapshot[str(int(gseq_text))] = name
    return tuple(sorted(snapshot.items(), key=lambda item: int(item[0])))


def _corroborated_deployment_group_names(
    owner: str,
    dseq: str,
    *,
    sources,
    reader,
    expected_group: str | None = None,
) -> list[str]:
    """Return all group names only when two independent chain sources agree.

    This is the destructive-path companion to :func:`deployment_group_names`.
    The latter deliberately accepts the first complete response because its other
    callers use an unreadable source only to hold or report a candidate. Selecting
    a mutating Console client needs a stronger statement: two HTTPS endpoints with
    distinct registered operators and gateway ancestries must return the same complete
    owner/DSEQ/group-id/name map.

    A malformed, truncated, missing, single-source, or disagreeing population is
    unknown and returns ``[]``. Console is not a vote in this consensus.
    """

    if not owner or not dseq:
        return []
    path = (
        f"{_DEPLOYMENT_API}/deployments/info"
        f"?id.owner={urllib.parse.quote(owner)}&id.dseq={urllib.parse.quote(dseq)}"
    )
    get = reader
    candidates = sources
    snapshots: list[tuple[tuple[str, str], ...]] = []
    source_ids: set[str] = set()
    hostnames: set[str] = set()
    operators: set[str] = set()
    ancestries: set[str] = set()
    cache_ancestries: set[str] = set()
    for source in candidates:
        if not isinstance(source, dict):
            return []
        base = source.get("url")
        source_id = source.get("source_id")
        operator = source.get("operator")
        ancestry = source.get("gateway_ancestry")
        cache_ancestry = source.get("cache_ancestry")
        identifiers = (source_id, operator, ancestry, cache_ancestry)
        if not all(
            isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", value)
            for value in identifiers
        ):
            return []
        if (
            source.get("chain_id") != "akashnet-2"
            or source.get("proof_mode") != "height-pinned-block-id-and-paginated-group-list"
            or source.get("finality_rule") != "committed-tip-minus-2"
            or source.get("max_age_seconds") != 180
            or source.get("max_height_skew") != 5
            or not isinstance(base, str)
        ):
            return []
        parsed = urllib.parse.urlsplit(base)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            return []
        if (
            not base.isascii()
            or parsed.scheme.lower() != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or hostname != (parsed.hostname or "")
            or port not in (None, 443)
        ):
            return []
        if (
            source_id in source_ids
            or hostname in hostnames
            or operator in operators
            or ancestry in ancestries
            or cache_ancestry in cache_ancestries
        ):
            return []
        source_ids.add(source_id)
        hostnames.add(hostname)
        operators.add(operator)
        ancestries.add(ancestry)
        cache_ancestries.add(cache_ancestry)
        try:
            data = get(path, base=base)
        except ChainResponseError:
            return []
        except Exception:  # noqa: BLE001,S112 — failed source contributes no authority
            continue
        if not isinstance(data, dict):
            return []
        snapshot = _deployment_group_snapshot(data, owner, dseq)
        if snapshot is None:
            return []
        snapshots.append(snapshot)
    if len(snapshots) < 2:
        return []
    if any(snapshot != snapshots[0] for snapshot in snapshots[1:]):
        return []
    if expected_group is not None and snapshots[0] != (("1", expected_group),):
        return []
    return [name for _gseq, name in snapshots[0]]


def corroborated_deployment_group_names(owner: str, dseq: str, expected_group: str) -> list[str]:
    """Closed-registry containment evidence; it is not fresh/finalized authority."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", dseq) or int(dseq) > 2**64 - 1:
        return []
    if os.environ.get("AKASH_REST_URL") is not None:
        return []
    if (
        _source_registry_digest(OWNER_CORROBORATION_SOURCES_V1)
        != OWNER_CORROBORATION_REGISTRY_SHA256
    ):
        return []
    return _corroborated_deployment_group_names(
        owner,
        dseq,
        sources=OWNER_CORROBORATION_SOURCES_V1,
        reader=_lcd_get,
        expected_group=expected_group,
    )


def _rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _block_hash(value: object) -> str | None:
    """Cosmos REST emits Tendermint block hashes as canonical base64, not hex."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 32 or base64.b64encode(raw).decode() != value:
        return None
    return raw.hex()


def _owner_close_evidence(
    owner: str,
    dseq: str,
    expected_group: str,
    *,
    sources,
    reader,
    now: datetime,
) -> dict[str, Any] | None:
    """Fresh same-height authority evidence, or None. Tests inject transport/time here."""
    # Validate the closed registry and exact singleton before doing the authority reads.
    # This unpinned observation is containment only and is never reused as authority.
    if _corroborated_deployment_group_names(
        owner,
        dseq,
        sources=sources,
        reader=reader,
        expected_group=expected_group,
    ) != [expected_group]:
        return None
    bases = [source.get("url") for source in sources if isinstance(source, dict)]
    if len(bases) != len(sources) or len(bases) < 2:
        return None

    malformed = object()

    def fetch(source, path, height=None):
        try:
            return reader(path, base=source["url"], height=height)
        except ChainResponseError:
            return malformed
        except Exception:  # noqa: BLE001 — transport failure abstains
            return None

    latest_path = "/cosmos/base/tendermint/v1beta1/blocks/latest"
    tips = []
    for source in sources:
        doc = fetch(source, latest_path)
        if doc is malformed:
            return None
        if doc is None:
            continue
        header = (doc.get("block") or {}).get("header") if isinstance(doc, dict) else None
        raw_height = header.get("height") if isinstance(header, dict) else None
        if not isinstance(raw_height, str) or not re.fullmatch(r"[1-9][0-9]*", raw_height):
            return None
        height = int(raw_height)
        if header.get("chain_id") != source.get("chain_id") or height <= 2:
            return None
        tips.append((source, height))
    if len(tips) < 2:
        return None
    if max(height for _source, height in tips) - min(height for _source, height in tips) > 5:
        return None
    height = min(value for _source, value in tips) - 2

    block_path = f"/cosmos/base/tendermint/v1beta1/blocks/{height}"
    blocks = []
    for source, _tip in tips:
        doc = fetch(source, block_path)
        if doc is malformed:
            return None
        if doc is None:
            continue
        header = (doc.get("block") or {}).get("header") if isinstance(doc, dict) else None
        block_id = doc.get("block_id") if isinstance(doc, dict) else None
        if not isinstance(header, dict) or not isinstance(block_id, dict):
            return None
        block_hash = _block_hash(block_id.get("hash"))
        block_time = _rfc3339(header.get("time"))
        if (
            header.get("chain_id") != source.get("chain_id")
            or str(header.get("height")) != str(height)
            or block_hash is None
            or block_time is None
        ):
            return None
        blocks.append((source, block_hash, block_time))
    if len(blocks) < 2:
        return None
    if any((item[1], item[2]) != (blocks[0][1], blocks[0][2]) for item in blocks[1:]):
        return None
    block_time = blocks[0][2]
    if now.tzinfo is None or now < block_time or (now - block_time).total_seconds() > 180:
        return None

    snapshot = None
    used = []
    for source, _observed_hash, _observed_time in blocks:
        # ``deployments/info`` carries a bare repeated field and cannot prove that a
        # sibling was omitted. Authority therefore requires a separate list contract
        # whose exhausted cursor and total positively reconcile with collected rows.
        # A source that does not implement this route/shape simply cannot authorize.
        base_path = (
            f"{_DEPLOYMENT_API}/groups/list"
            f"?filters.owner={urllib.parse.quote(owner)}"
            f"&filters.dseq={urllib.parse.quote(dseq)}"
            "&pagination.limit=100&pagination.count_total=true"
        )
        rows = []
        cursor = None
        total = None
        seen_cursors = set()
        transport_failed = False
        for _page in range(100):
            page_path = base_path
            if cursor is not None:
                page_path += f"&pagination.key={urllib.parse.quote(cursor)}"
            doc = fetch(source, page_path, height=height)
            if doc is malformed:
                return None
            if doc is None:
                transport_failed = True
                break
            if not isinstance(doc, dict) or not isinstance(doc.get("groups"), list):
                return None
            pagination = doc.get("pagination")
            if not isinstance(pagination, dict):
                return None
            raw_total = pagination.get("total")
            if not isinstance(raw_total, str) or not re.fullmatch(r"[0-9]+", raw_total):
                return None
            page_total = int(raw_total)
            if page_total > 10_000 or (total is not None and page_total != total):
                return None
            total = page_total
            rows.extend(doc["groups"])
            if len(rows) > total:
                return None
            next_key = pagination.get("next_key")
            if next_key in (None, ""):
                if len(rows) != total:
                    return None
                break
            if not isinstance(next_key, str) or next_key in seen_cursors or len(rows) >= total:
                return None
            seen_cursors.add(next_key)
            cursor = next_key
        else:
            return None
        if transport_failed:
            continue
        current = _group_rows_snapshot(rows, owner, dseq)
        if current is None:
            return None
        if snapshot is None:
            snapshot = current
        elif current != snapshot:
            return None
        used.append(source["source_id"])
    if len(used) < 2 or snapshot != (("1", expected_group),):
        return None
    population = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True)
    expires = min(now.timestamp() + 30, block_time.timestamp() + 180)
    if expires <= now.timestamp():
        return None
    return {
        "evidence_version": 1,
        "registry_version": OWNER_CORROBORATION_REGISTRY_VERSION,
        "registry_digest": OWNER_CORROBORATION_REGISTRY_SHA256,
        "registry_provenance": OWNER_CORROBORATION_REGISTRY_PROVENANCE,
        "registry_provenance_digest": OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256,
        "chain_id": "akashnet-2",
        "source_ids": used,
        "height": height,
        "block_hash": blocks[0][1],
        "block_time": block_time.isoformat(),
        "owner": owner,
        "dseq": dseq,
        "gseq": "1",
        "group": expected_group,
        "population_digest": hashlib.sha256(population.encode()).hexdigest(),
        "observed_at": now.isoformat(),
        "evaluated_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
    }


def owner_close_evidence(owner: str, dseq: str, expected_group: str) -> dict[str, Any] | None:
    """Closed-registry fresh authority evidence for one exact owner/DSEQ/group."""
    if (
        not isinstance(dseq, str)
        or not re.fullmatch(r"[1-9][0-9]{0,19}", dseq)
        or int(dseq) > 2**64 - 1
    ):
        return None
    if not isinstance(expected_group, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", expected_group):
        return None
    if not isinstance(owner, str) or not re.fullmatch(r"akash1[a-z0-9]{38,58}", owner):
        return None
    if os.environ.get("AKASH_REST_URL") is not None:
        return None
    if (
        _source_registry_digest(OWNER_CORROBORATION_SOURCES_V1)
        != OWNER_CORROBORATION_REGISTRY_SHA256
    ):
        return None
    return _owner_close_evidence(
        owner,
        dseq,
        expected_group,
        sources=OWNER_CORROBORATION_SOURCES_V1,
        reader=_lcd_get,
        now=datetime.now(timezone.utc),
    )


def _coins_map(coins: list[dict[str, Any]]) -> dict[str, int]:
    """Sum a list of {denom, amount} into {denom: int_amount}. Amounts arrive as
    integer strings; some nodes append a decimal suffix (``"170623558.000…"``), so
    the integer part is parsed directly — never via float(), which would silently
    round large micro-unit balances."""
    out: dict[str, int] = {}
    for c in coins or []:
        denom = c.get("denom")
        raw = c.get("amount")
        if not denom or raw is None:
            continue
        try:
            amt = int(str(raw).split(".", 1)[0])  # drop any ".000…" suffix, parse as int
        except (TypeError, ValueError):
            continue
        out[denom] = out.get(denom, 0) + amt
    return out


def _deposit_grant_breakdown(
    data: dict[str, Any],
) -> list[tuple[dict[str, int], str | None]]:
    """One ``(coins, expiration)`` per DepositAuthorization grant in `data`.

    The chain payload lists every authz grant to the address. The deploy credit
    is the grant with the LATEST ``expiration`` — that is the granter currently
    funding the account. Earlier grants have been SUPERSEDED by a new depositor
    and remain on-chain until they lapse, but their ``spend_limit`` is dead: the
    depositor no longer funds them, so any remaining allowance is unreachable.

    Summing all grants double-counts (dead + fresh). Max-among-grants picks the
    dead one when its remaining allowance happens to be larger — which is the
    exact bug a real grant-supersession produces: a chain measured today has an
    old grant of 1000 ACT expiring 2027-01-01 and a fresh grant of 50 ACT
    expiring 2030-01-01, and max returns 1000 ACT while the live wallet has 50.

    `expiration` is None iff the chain payload did not include the field
    (malformed response, or a grant that genuinely never lapses — the chain
    never returns the latter, but the type allows it). The freshness
    discriminator requires the field, so a grant without it cannot contribute
    to "which is freshest" and is excluded — but that exclusion is a state, not
    a silent loss; see ``deploy_credit`` for the three-way contract handling.
    """
    out: list[tuple[dict[str, int], str | None]] = []
    for grant in data.get("grants", []) or []:
        auth = grant.get("authorization", {})
        if auth.get("@type") != _DEPOSIT_AUTH_TYPE:
            continue
        coins = _coins_map(auth.get("spend_limits") or [])
        out.append((coins, grant.get("expiration")))
    return out


def _parse_expiration(value: str) -> datetime | None:
    """Parse a Cosmos RFC3339 ``expiration`` to a tz-aware UTC datetime.

    REQUIRED for the freshness discriminator: the chain returns expiration as a
    string, and different endpoints emit DIFFERENT SURFACE FORMS of the SAME
    instant — ``"2030-01-01T00:00:00Z"`` vs ``"2030-01-01T00:00:00.000Z"`` vs
    ``"2030-01-01T00:00:00+00:00"``. A string-based ``max()`` over these is
    LEXICOGRAPHIC, not chronological: ``ord('.') == 46`` and ``ord('Z') == 90``,
    so ``"...00Z"`` sorts AFTER ``"...00.000Z"`` — the whole-second form is
    treated as LATER than the fractional form, even when the fractional form
    is 1ms LATER in time. Picking the wrong one is the same defect class as
    #168, one layer up: max(amount) became max(string), and both lie about
    freshness.

    Returns ``None`` on any parse failure. ``None`` is the "could not ask"
    state for this accessor — the caller (deploy_credit) routes it to the
    three-way contract exclusion list with ``warnings.warn``, never into the
    freshness discriminator. Returning ``None`` instead of raising here
    keeps the helper composable and the error message in one place.

    Accepts the surface forms measured in production:
      * ``2030-01-01T00:00:00Z``                  — whole second, UTC
      * ``2030-01-01T00:00:00.001Z``              — fractional, UTC
      * ``2030-01-01T00:00:00.123456Z``           — microseconds, UTC
      * ``2030-01-01T00:00:00+00:00``             — explicit offset
    """
    if not isinstance(value, str) or not value:
        return None
    s = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        # A naive datetime has no instant — comparing it against a tz-aware
        # one in `max()` would raise TypeError. Treat as un-parseable.
        return None
    return dt.astimezone(timezone.utc)


def deploy_credit(address: str) -> dict[str, int]:
    """Remaining Console deploy credit for ``address``, as {denom: micro_amount}.

    Reads every escrow ``DepositAuthorization`` granted TO this account, picks
    the grant with the LATEST ``expiration`` (the fresh depositor's grant),
    and returns ITS ``spend_limits``. Earlier grants are SUPERSEDED — they
    remain on-chain until they lapse, but their remaining allowance is dead.
    A larger amount from an earlier-expiring grant is not more money; it is a
    different, dead grant.

    ⛔ NOT max-across-endpoints, NOT sum-across-grants. The OLD rule was
    ``totals[denom] = max(totals.get(denom, 0), amt)``: "staleness can only
    lose a deposit, never invent one, so the highest reading is the freshest".
    FALSE when a grant has been REPLACED — the OLD (superseded) grant keeps a
    fixed ``spend_limit`` until it lapses, while the NEW grant starts at a
    smaller amount; max picks the OLD, dead grant. Measured today on
    ``akash1me``:

        api.akashnet.net        407.85 ACT   (expiration 2036-08-04)  ← fresh
        akash-api.polkachu.com  407.85 ACT   (expiration 2036-08-04)  ← fresh
        akash-rest.publicnode.com 246.19 ACT (expiration 2036-07-14)  ← superseded

    In a chain where the supersession is the OPPOSITE shape (the old grant
    happens to have a larger remaining allowance than the new one), max picks
    the dead grant and over-reports deploy credit by the OLD allowance —
    every gate that read deploy credit reads a phantom balance. #168.

    ⛔ AND the freshness discriminator compares PARSED DATETIMES, not raw
    RFC3339 strings. ``max("2030-01-01T00:00:00Z", "2030-01-01T00:00:00.001Z")``
    returns the WHOLE-SECOND form because ``ord('Z') == 90`` > ``ord('.') == 46`` —
    a lexicographic comparison, not chronological. The moment one endpoint
    emits fractional seconds and another does not, the string-max selects the
    SUPERSEDED grant. #168, second take.

    Reconciles across endpoints:
      * Flatten every grant from every endpoint into ``(coins, expiration, source)``.
      * Parse each ``expiration`` to a tz-aware datetime via
        ``_parse_expiration``. Un-parseable or missing expirations route to
        the three-way contract below.
      * Pick the grant with the LATEST ``expiration`` (datetime comparison) —
        the fresh depositor.
      * Ties on ``expiration`` (same datetime across endpoints) break by MAX
        ``uact`` — staleness-only, no supersession. Tied amounts across all
        denoms (uakt rides along at 0 in every grant and is harmless).

    Three-way contract on missing or un-parseable ``expiration``
    (akash-lease-core #18): the field is required to discriminate fresh from
    superseded, so a grant without it (or with a value that does not parse as
    RFC3339) is "could not ask" — must NOT silently win or silently lose:
      * If EVERY grant (across every endpoint) lacks a parseable
        ``expiration``: raise with the list of sources, so a caller gates
        destructively.
      * If SOME have a parseable expiration and some do not: use the ones
        that do (the freshness discriminator is sound) and emit a
        ``warnings.warn`` naming the excluded sources — they contributed 0
        to the answer, not silently, by being named.
    """
    import warnings

    errors: list[str] = []
    bases = rest_urls()

    def _one(base: str) -> tuple[str, list[tuple[dict[str, int], str | None]], str]:
        try:
            data = _lcd_get(f"/cosmos/authz/v1beta1/grants/grantee/{address}", base=base)
        except RuntimeError as e:  # one dead LCD must not sink the reading
            return base, [], str(e)
        return base, _deposit_grant_breakdown(data), ""

    # CONCURRENT, because the timeouts add up. Queried in sequence, three dead endpoints
    # at the 15s _lcd_get timeout block for ~45s — and this call sits in front of every
    # deploy, so a slow reading looks like a hung CI job. Fanning out costs one thread
    # each and bounds the wait at the slowest single endpoint.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(bases))) as pool:
        per_endpoint: list[tuple[str, list[tuple[dict[str, int], str | None]]]] = []
        for base, breakdown, err in pool.map(_one, bases):
            if err:
                errors.append(f"{base}: {err}")
            else:
                per_endpoint.append((base, breakdown))
    if not per_endpoint:
        raise RuntimeError(
            "no LCD endpoint could be reached for deploy credit: " + "; ".join(errors)
        )
    # Flatten across endpoints, tagging each grant with its source.
    all_grants: list[tuple[dict[str, int], str | None, str]] = []
    for base, breakdown in per_endpoint:
        for coins, exp in breakdown:
            all_grants.append((coins, exp, base))
    if not all_grants:
        return {}  # every endpoint returned 200 but no DepositAuthorization grants
    # Three-way contract: a grant whose `expiration` is missing OR does not
    # parse as RFC3339 is "could not ask" for the freshness discriminator —
    # we cannot tell fresh from superseded, so it cannot contribute. Surface
    # the state, never silently lose. The un-parseable grants share the same
    # warning channel as the missing ones; both are excluded for the SAME
    # reason (the discriminator cannot use them), and treating them
    # uniformly is what keeps the three-way contract simple.
    grants_with_exp: list[tuple[dict[str, int], datetime, str]] = []
    grants_without_exp: list[tuple[dict[str, int], str]] = []
    for coins, exp, source in all_grants:
        parsed = _parse_expiration(exp) if exp else None
        if parsed is None:
            grants_without_exp.append((coins, source))
        else:
            grants_with_exp.append((coins, parsed, source))
    if not grants_with_exp:
        sources = sorted({s for _, s in grants_without_exp})
        raise RuntimeError(
            "every LCD returned DepositAuthorization grants WITHOUT a parseable "
            "`expiration` field; cannot reconcile by LATEST EXPIRATION (the "
            "discriminator that distinguishes a fresh grant from a superseded "
            "one). Sources: " + ", ".join(sources)
        )
    # LATEST EXPIRATION wins (datetime comparison, NOT string). Ties: pick
    # the coins map with the MAX uact (one endpoint indexed a deposit the
    # other hasn't yet — same expiry, higher uact = fresh reading). Other
    # denoms ride along at 0 and don't affect the tie-break.
    latest_exp = max(e for _, e, _ in grants_with_exp)
    fresh_readings = [(c, s) for c, e, s in grants_with_exp if e == latest_exp]
    chosen = max(fresh_readings, key=lambda cs: cs[0].get("uact", 0))[0]
    # VISIBILITY: the reconciliation above is correct whether the endpoints
    # agree or not — and that is the trap. Measured 2026-08-29 on
    # akash1n4uut3…: publicnode served a superseded 170.62-ACT grant
    # (expiration 2036-07-08) all day while akashnet + polkachu agreed on the
    # fresh 116.33-ACT vintage (2036-08-24). The rule silently picked the
    # right value, a 54-ACT phantom sat in the fleet's key ranking, and
    # nothing anywhere said "these LCDs disagree". So when the endpoints'
    # CHOSEN vintages differ — or a fan-out member was unreachable — SAY SO.
    # Per-endpoint choice = that endpoint's latest-expiration grant; in the
    # common case every endpoint reports BOTH grants and chooses identically,
    # so unanimous chains stay silent (see the no-noise test).
    per_endpoint_choice: list[tuple[str, int, datetime]] = []
    # ⛔ AN ENDPOINT WITH NO SELECTABLE GRANT IS A DISAGREEMENT, NOT AN ABSENCE. These
    # were dropped by `if endpoint_grants:` before the message was built, so an LCD that
    # ANSWERED and reported no usable deploy grant — while its peers reported one — was
    # silently missing from a warning whose whole job is to name who disagrees. That is
    # the strongest disagreement available and it was the one form it could not print.
    barren: list[str] = []
    for base, breakdown in per_endpoint:
        endpoint_grants: list[tuple[dict[str, int], datetime]] = []
        for coins, exp in breakdown:
            parsed = _parse_expiration(exp) if exp else None
            if parsed is not None:
                endpoint_grants.append((coins, parsed))
        if not endpoint_grants:
            barren.append(base)
        if endpoint_grants:
            # ⛔ TIE-BREAK ON uact, EXACTLY AS THE GLOBAL RECONCILIATION ABOVE DOES.
            # Keying on expiration alone made `max` return the FIRST maximal element, so
            # when an endpoint served two grants with the SAME expiration the PAYLOAD
            # ORDER picked the winner. Two endpoints holding the identical pair,
            # serialised in opposite order, then "chose" different uact and this reported
            # a DISAGREE about data that was byte-equal as a set. Two selections compared
            # against each other have to use one rule.
            coins, exp = max(endpoint_grants, key=lambda ce: (ce[1], ce[0].get("uact", 0)))
            per_endpoint_choice.append((base, coins.get("uact", 0), exp))
    # ⛔ THE PAIR, NOT THE AMOUNT. Comparing only `uact` misses endpoints that agree on
    # the figure while having chosen DIFFERENT grant vintages — same money, different
    # expiration, and the vintage is what decides whether the grant is live or superseded.
    # A silent reconciliation of exactly that kind hid a 54-ACT phantom. Caught on #223.
    # A barren endpoint only means something ALONGSIDE one that did select a grant; if
    # nothing anywhere has a grant there is no disagreement, just no data.
    if len({(u, e) for _, u, e in per_endpoint_choice}) > 1 or (barren and per_endpoint_choice):
        # ⛔ PRINT THE WHOLE INSTANT, AT THE COMPARISON'S OWN RESOLUTION. `%Y-%m-%d`
        # rendered endpoints differing by HOURS as the SAME STRING. Fixing that with
        # `%Y-%m-%dT%H:%M:%S%z` moved the defect rather than removing it: `_parse_expiration`
        # preserves MICROSECONDS and the comparison is over full datetimes, so
        # `…00.000Z` and `…00.001Z` still rendered identically —
        #     2036-08-24T22:00:00+0000  |  2036-08-24T22:00:00+0000
        # measured. `isoformat()` is the only rendering that cannot fall behind the
        # comparison, because it carries whatever precision the datetime holds:
        #     2036-08-24T22:00:00+00:00 | 2036-08-24T22:00:00.001000+00:00
        # A warning that fires correctly and offers two identical values as its evidence
        # reads as a bug in the warning. Any FIXED format string re-opens this the moment
        # the parser gains precision; the message must follow the comparison, not a
        # snapshot of it.
        detail = "; ".join(
            f"{base}={uact}uact@{exp.isoformat()}" for base, uact, exp in per_endpoint_choice
        )
        if barren:
            detail += "; " + "; ".join(f"{base}=NO SELECTABLE GRANT" for base in barren)
        warnings.warn(
            f"deploy_credit: LCDs DISAGREE on the deploy grant — {detail}. "
            f"Kept the latest-expiration vintage ({latest_exp:%Y-%m-%d}); an endpoint "
            "holding the LARGER figure on an OLDER expiration is serving a "
            "superseded grant, not more money.",
            stacklevel=2,
        )
    if errors:
        warnings.warn(
            f"deploy_credit: {len(errors)} endpoint(s) unreachable during the grant "
            f"read: {'; '.join(errors)}. Answer rests on the "
            f"{len(per_endpoint)} endpoint(s) that answered.",
            stacklevel=2,
        )
    # Surface (do not silently lose) grants whose freshness we could not
    # verify. ``warnings.warn`` is the documented channel — callers can
    # filter with ``warnings.simplefilter("error")`` if they want a hard gate.
    if grants_without_exp:
        excluded = sorted({s for _, s in grants_without_exp})
        warnings.warn(
            f"deploy_credit: {len(grants_without_exp)} grant(s) from {excluded} "
            "had no parseable `expiration` and were excluded from the freshness "
            "discriminator (not silently lost — named here). Re-run on a healthier "
            "LCD if this is unexpected.",
            stacklevel=2,
        )
    return chosen


def granted_uact(
    address: str, *, quorum: tuple[str, ...] | None = None, height: int | None = None
) -> int | None:
    """Canonical uact accessor for callers that need an explicit quorum contract.

    ``deploy_credit`` remains the backwards-compatible rich result. This narrow API
    returns only plural ``spend_limits[uact]`` and never converts an unreadable grant
    into zero. The optional arguments are retained as the integration seam for the
    pinned-height reader used by CI selectors.
    """
    bases = list(quorum or tuple(rest_urls()))
    if not bases:
        return None
    if height is None:
        try:
            tip = int(
                _lcd_get("/cosmos/base/tendermint/v1beta1/blocks/latest", base=bases[0])["block"][
                    "header"
                ]["height"]
            )
        except (RuntimeError, KeyError, TypeError, ValueError):
            return None
        height = tip - 3
    if height <= 0:
        return None
    readings: list[int] = []
    outcomes: list[tuple[str, int | None, str]] = []  # (base, uact | None, skip reason)
    for base in bases:
        try:
            value = _sum_deposit_grants(
                _lcd_get(
                    f"/cosmos/authz/v1beta1/grants/grantee/{address}", base=base, height=height
                )
            ).get("uact")
        except RuntimeError as e:
            outcomes.append((base, None, str(e)))
            continue
        if value is not None:
            readings.append(value)
            outcomes.append((base, value, ""))
        else:
            outcomes.append((base, None, "no parseable uact grant"))
    if not readings:
        return None
    counts = {value: readings.count(value) for value in set(readings)}
    agreeing = [value for value, count in counts.items() if count >= 2]
    if not agreeing:
        return None
    # FAIL-SAFE on an even split (CodeRabbit, #222): with a four-member quorum
    # reading 2-2, BOTH values satisfy `count >= 2` and max() would return the
    # LARGER — the optimistic direction this module exists to prevent. min()
    # under-reports; the TIED warning below tells the operator why.
    result = min(agreeing)
    # VISIBILITY, same contract as deploy_credit: a correct-but-silent quorum
    # hides the split. Name every member that was excluded (could not serve
    # the pinned height — the measured case is the DEFAULT LCD, which ignores
    # the x-cosmos-block-height pin and must be skipped every single call)
    # and every reading that dissented from the majority. The operator sees
    # WHO was not counted; the value still comes from the agreeing pair.
    import warnings

    excluded = [(b, r) for b, v, r in outcomes if v is None]
    # Classify against `result`, NOT membership in `agreeing`: on an even
    # split both values qualify and the losing pair would be labelled
    # "agreeing" while differing from the canonical answer (CodeRabbit, #222).
    dissent = [(b, v) for b, v, r in outcomes if v is not None and v != result]
    if excluded or dissent:
        parts: list[str] = []
        if excluded:
            listed = ", ".join(f"{b} ({r[:80]})" for b, r in excluded)
            parts.append(f"quorum excluded {len(excluded)} endpoint(s): {listed}")
        if dissent:
            tied = any(counts[v] == counts[result] for _, v in dissent)
            listed = ", ".join(f"{b}={v}uact" for b, v in dissent)
            parts.append(f"{'TIED equal-majority split; ' if tied else ''}dissent: {listed}")
        n_agree = counts[result]
        warnings.warn(
            f"granted_uact: {'; '.join(parts)} — canonical {result} uact from "
            f"{n_agree}/{len(bases)} agreeing endpoint(s) at height {height}.",
            stacklevel=2,
        )
    return result


def free_uact(granted_uact_value: int) -> int:
    """Free deploy credit in uact, derived from the DepositAuthorization spend_limit.

    ⭐ Fix for #169: ``spend_limits`` is ALREADY NET of locked escrow. The Cosmos
    authz module decrements ``spend_limits`` as the grantee uses escrow, so the
    value the chain returns is the *remaining* allowance, NOT the gross grant.
    Subtracting a separately-measured ``locked_uact`` double-subtracts and clamps
    to 0 — so a 90 ACT account with 346 ACT in escrow reads ``free_uact = 0``,
    firing the low-credit alarm permanently on a funded wallet.

    The OLD expression ``max(granted_uact - locked_uact, 0)`` is wrong. The
    correct expression is the spend_limit value itself (clamped to 0 defensively):

    - **Two independent payload-level disproofs (from the issue):**
      1. ``locked > granted`` is routine on real accounts
         (e.g. akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee: granted=90.23 ACT,
         locked=346.43 ACT). A gross grant could not allow more escrow to be
         locked than was ever granted. ``spend_limits`` must be net.
      2. ``spend_limits`` falls in exact 5 ACT steps as deployments are created
         (measured: ``25.670005 -> 15.670001`` = -10.000004 on 2 deposits,
         ``15.670001 -> 10.662414`` = -5.007587 on 1 deposit). A deposit's
         escrow cost is 5 ACT; only a *remaining allowance* decreases by that
         exact amount. A gross grant does not move when a deposit is taken.

    - **Where this is used:** ``cli.py:999`` (deploy-credit-check path) and
      ``cli.py:1097`` (wallet-balance path). Both sites previously computed
      ``free_uact = max(granted_uact - locked_uact, 0)`` — the bug. They now
      call this helper.

    ``locked_in_escrow_uact`` is still useful as a DISPLAY field (how much is
    parked in escrow right now) — keep emitting it in payloads. It is just
    not a subtrahend of free credit.
    """
    if granted_uact_value < 0:
        return 0
    return granted_uact_value


def _sum_deposit_grants(data: dict[str, Any]) -> dict[str, int]:
    """Sum uact spend_limits across DepositAuthorization grants in one LCD payload."""
    totals: dict[str, int] = {}
    for grant in data.get("grants", []) or []:
        auth = grant.get("authorization", {})
        if auth.get("@type") != _DEPOSIT_AUTH_TYPE:
            continue
        # The chain carries a singular `spend_limit` uakt decoy alongside the
        # plural DepositAuthorization allowance. Never fall back to the singular
        # field: treating it as a list reports zero deploy credit for a funded
        # Console AUTHZ grantee.
        limits = auth.get("spend_limits")
        for denom, amt in _coins_map(limits or []).items():
            totals[denom] = totals.get(denom, 0) + amt
    return totals


def credit_grant_detail(address: str) -> dict[str, Any] | None:
    """The escrow DepositAuthorization granted to ``address`` (granter + expiration),
    or None if there is none. Diagnostic detail for the wallet report — which managed
    wallet funds this account, and when the authorization lapses."""
    data = _lcd_get(f"/cosmos/authz/v1beta1/grants/grantee/{address}")
    for grant in data.get("grants", []) or []:
        if grant.get("authorization", {}).get("@type") == _DEPOSIT_AUTH_TYPE:
            return {
                "granter": grant.get("granter"),
                "grantee": grant.get("grantee"),
                "expiration": grant.get("expiration"),
            }
    return None


def bank_balances(address: str) -> dict[str, int]:
    """Liquid on-chain balance for ``address`` as {denom: micro_amount}. Usually empty
    for a Console-managed account (funds live as the credit grant, not liquid AKT)."""
    data = _lcd_get(f"/cosmos/bank/v1beta1/balances/{address}")
    return _coins_map(data.get("balances", []))


def format_amount(denom: str, micro: int) -> str:
    """Render a micro-unit amount as e.g. '170.62 ACT'. Unknown denoms pass through
    with their raw denom so nothing is silently mislabeled."""
    meta = _DENOM_META.get(denom)
    if not meta:
        return f"{micro} {denom}"
    value = micro / (10 ** meta["decimals"])
    return f"{value:,.2f} {meta['label']}"


def usd_estimate(denom: str, micro: int) -> float | None:
    """USD estimate for a USD-pegged denom (uact ≈ $1/ACT), else None. Never guesses a
    price for AKT — that floats — so callers only show '$' when it's actually pegged."""
    meta = _DENOM_META.get(denom)
    if not meta or not meta.get("usd_pegged"):
        return None
    return round(micro / (10 ** meta["decimals"]), 2)


def describe_coins(coins: dict[str, int]) -> list[dict[str, Any]]:
    """Turn {denom: micro} into display rows for the CLI/JSON, dropping zero amounts
    (a DepositAuthorization carries a 0-uakt entry beside the real uact limit). Sorted
    largest-first so the meaningful balance leads."""
    rows = [
        {
            "denom": denom,
            "micro": micro,
            "display": format_amount(denom, micro),
            "usd_estimate": usd_estimate(denom, micro),
        }
        for denom, micro in coins.items()
        if micro > 0
    ]
    rows.sort(key=lambda r: r["micro"], reverse=True)
    return rows


def corroborate_listing(
    listing_is_empty: bool, chain_active: int | None, address: str = ""
) -> list[str]:
    """Why an empty Console listing must not be reported as a clean fleet.

    Returns the degradation reasons; empty list means the result stands on its own.

    ⛔ THE THREE EMPTY CASES ARE NOT ONE CASE. An empty listing can mean the fleet is
    clean, that the listing is incomplete, or that nobody could check — and all three
    print `closeable_count: 0`. Only the first is an all-clear.

      listing non-empty            -> []                      (nothing to corroborate)
      empty + chain says N>0       -> [mismatch]              (the listing is lying)
      empty + chain says 0         -> []                      (corroborated clean)
      empty + chain unreadable     -> [unconfirmed]           (an unasked question)

    ⚠ `chain_active == 0` and `chain_active is None` MUST stay distinguishable here.
    Collapsing "could not ask" into "zero" would confirm an empty listing with an empty
    answer — which is the exact defect this function exists to prevent (#208).
    """
    if not listing_is_empty:
        return []
    if chain_active is None:
        return [
            "Console listing returned 0 deployments and the chain could not be "
            "read to corroborate it. UNCONFIRMED, not clean."
        ]
    if chain_active > 0:
        return [
            f"Console listing returned 0 deployments for {address}, but the chain "
            f"reports {chain_active} ACTIVE. The listing is incomplete, so "
            f"'closeable_count: 0' is NOT an all-clear — it is an unasked question."
        ]
    return []


def latest_height(timeout: int = 15) -> int | None:
    """The chain's current block height, or None when it cannot be read.

    ⛔ None, never 0. Height is the denominator of every age computation here; a 0 would
    make every deployment look infinitely old, which is the direction that closes live
    escrow. An unreadable height must make ages UNKNOWN, not ancient.
    """
    try:
        data = _lcd_get("/cosmos/base/tendermint/v1beta1/blocks/latest", timeout=timeout)
    except RuntimeError:
        return None
    try:
        return int(data["block"]["header"]["height"])
    except (KeyError, TypeError, ValueError):
        return None
