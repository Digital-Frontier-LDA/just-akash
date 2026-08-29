"""Bid-health probe — does each of OUR providers still bid, on every order shape we sell?

This is the outside-in replacement for the autobidder's in-cluster synthetic
probe (``scripts/synthetic_probe.py`` in DePIN-LiveAutobidder). Same question,
different vantage: submit a real order shaped like a customer's, wait for OUR
provider's bid, close the order, record the verdict. It never creates a lease —
the bid IS the signal — so it costs escrow churn and no lease spend.

Why it exists at all: the provider holds ONE long-lived websocket for order
events and an unclean RPC restart leaves it half-open, looking healthy while
bidding on nothing (4 silent full-bid outages Jun-Jul 2026). Nothing else in
the fleet notices, because a provider that bids on nothing emits no errors.

Three properties are load-bearing and were learned the hard way:

1. **Placement attributes pin the order to one provider.** Filtering bids by
   address client-side is not enough: a public order attracts 20+ bidders and
   Akash caps the bid set at 20, so ours can lose the race and read as a
   NO-BID that never happened. The attributes make everyone else ineligible.

2. **"Couldn't test" is not "failed".** Every eligible pair emits
   ``just_akash_bidprobe_skipped`` on EVERY run — 0 when the pair was genuinely
   tested, 1 when something on our side (no credit, index lag, probe error)
   made the answer meaningless. An alert that cannot tell those apart pages the
   operator for its own broken plumbing; on 2026-08-13 a drained probe wallet
   did exactly that on all three clusters at once. The zero case matters as
   much as the one case: a metric that is only emitted when skipped leaves the
   alert's join factor absent, and `x * on(...) (skipped == bool 0)` silently
   evaluates to nothing rather than to a page.

3. **A credit lapse must be loud, not empty.** When Console credit runs out we
   emit ``skipped=1`` for every remaining pair rather than emitting nothing at
   all. Emitting nothing looks identical to "the run never happened", which the
   staleness rule catches hours later; emitting skipped=1 is visible now.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Scenarios — ported from DePIN-LiveAutobidder src/synthetic_probe_scenarios.py
#
# Kept byte-compatible in shape with the in-cluster probe's SDLs so that during
# the parallel soak the two systems are asking the provider the SAME question.
# Diverging these while comparing verdicts would make the comparison worthless.
# ---------------------------------------------------------------------------

# A stocked model missing from this list lets the probe submit while all of its
# eligible models are sold out, and the provider's correct "insufficient
# capacity" decline pages a false critical (2026-07-05: every rtx4000ada +
# rtx3090ti was leased while v100/t4/p40/p4/m4000 sat free). Keep in sync with
# the cluster's real inventory.
GPU_PROBE_MODELS: tuple[str, ...] = (
    "rtx4000ada",
    "rtx3090ti",
    "v100",
    "t4",
    "p40",
    "p4",
    "m4000",
)

_SDL_CPU = """\
---
version: "2.0"
services:
  probe:
    image: alpine:3.19
    command: ["sh", "-c", "sleep 60"]
    expose:
      - port: 80
        as: 80
        to:
          - global: true
profiles:
  compute:
    probe:
      resources:
        cpu:
          units: 1
        memory:
          size: 512Mi
        storage:
          size: 128Mi
  placement:
    akash:
      pricing:
        probe:
          denom: uact
          amount: 1000000
deployment:
  probe:
    akash:
      profile: probe
      count: 1
"""

_SDL_GPU = """\
---
version: "2.0"
services:
  probe:
    image: alpine:3.19
    command: ["sh", "-c", "sleep 60"]
    expose:
      - port: 80
        as: 80
        to:
          - global: true
profiles:
  compute:
    probe:
      resources:
        cpu:
          units: 1
        memory:
          size: 512Mi
        storage:
          size: 256Mi
        gpu:
          units: 1
          attributes:
            vendor:
              nvidia:
{models}
  placement:
    akash:
      pricing:
        probe:
          denom: uact
          amount: 5000000
deployment:
  probe:
    akash:
      profile: probe
      count: 1
""".format(models="\n".join(f"                - model: {m}" for m in GPU_PROBE_MODELS))

_SDL_PERSISTENT_BETA3 = """\
---
version: "2.0"
services:
  probe:
    image: alpine:3.19
    command: ["sh", "-c", "sleep 60"]
    expose:
      - port: 80
        as: 80
        to:
          - global: true
    params:
      storage:
        data:
          mount: /data
profiles:
  compute:
    probe:
      resources:
        cpu:
          units: 1
        memory:
          size: 512Mi
        storage:
          - size: 128Mi
          - name: data
            size: 1Gi
            attributes:
              persistent: true
              class: beta3
  placement:
    akash:
      pricing:
        probe:
          denom: uact
          amount: 2000000
deployment:
  probe:
    akash:
      profile: probe
      count: 1
"""

_SDL_IP_LEASE = """\
---
version: "2.0"
endpoints:
  web:
    kind: ip
services:
  probe:
    image: alpine:3.19
    command: ["sh", "-c", "sleep 60"]
    expose:
      - port: 80
        as: 80
        to:
          - global: true
            ip: web
profiles:
  compute:
    probe:
      resources:
        cpu:
          units: 1
        memory:
          size: 512Mi
        storage:
          size: 128Mi
  placement:
    akash:
      pricing:
        probe:
          denom: uact
          amount: 1500000
deployment:
  probe:
    akash:
      profile: probe
      count: 1
"""


@dataclass(frozen=True)
class Scenario:
    name: str
    sdl: str


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario("cpu", _SDL_CPU),
        Scenario("gpu", _SDL_GPU),
        Scenario("persistent-beta3", _SDL_PERSISTENT_BETA3),
        Scenario("ip-lease", _SDL_IP_LEASE),
    )
}


@dataclass(frozen=True)
class ProviderTarget:
    """One of our providers, and the order shapes it is expected to bid on.

    ``attributes`` must narrow the order to THIS provider alone — they are the
    pinning mechanism described in the module docstring, not decoration. They
    mirror each cluster's live PROBE_ATTRIBUTES dotenv value.

    ``capabilities`` mirrors CLUSTER_CAPABILITIES in the autobidder repo. A
    cluster missing from that map made its in-cluster probe die silently for 18
    days (hetzner_hel, 2026-06/07), so here an unknown cluster is a hard error
    rather than an empty scenario list.
    """

    cluster: str
    wallet: str
    capabilities: frozenset[str]
    attributes: dict[str, str] = field(default_factory=dict)


PROVIDERS: tuple[ProviderTarget, ...] = (
    ProviderTarget(
        cluster="alphavps",
        wallet="akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",  # pragma: allowlist secret
        capabilities=frozenset({"cpu", "persistent-beta3", "ip-lease"}),
        attributes={"region": "eu-east", "organization": "digital frontier"},
    ),
    ProviderTarget(
        cluster="onidc",
        wallet="akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",  # pragma: allowlist secret
        capabilities=frozenset({"cpu", "gpu", "persistent-beta3", "ip-lease"}),
        attributes={
            "region": "eu-west",
            "organization": "digital frontier",
            "hosting-provider": "oni",
        },
    ),
    ProviderTarget(
        cluster="hetzner_hel",
        wallet="akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x",  # pragma: allowlist secret
        capabilities=frozenset({"cpu", "persistent-beta3"}),
        attributes={"region": "eu-north", "organization": "digital frontier"},
    ),
)


def eligible_pairs(
    providers: Iterable[ProviderTarget] = PROVIDERS,
) -> list[tuple[ProviderTarget, Scenario]]:
    """Every (provider, scenario) this fleet can legitimately be asked about.

    Order is stable so a run's output is diffable against the previous run.

    An unknown capability is a hard error, not a silent no-op. A capability
    that matches no scenario means that order shape is never probed, and the
    exported series simply never appears — which reads as "nothing wrong"
    forever. That exact failure (a cluster missing from the capability map)
    left hetzner_hel's in-cluster probe dead for 18 days while its metric sat
    frozen green.
    """
    pairs: list[tuple[ProviderTarget, Scenario]] = []
    for p in providers:
        unknown = sorted(p.capabilities - set(SCENARIOS))
        if unknown:
            raise ValueError(
                f"{p.cluster}: capabilities {unknown} match no scenario "
                f"(known: {sorted(SCENARIOS)}). A typo here silently stops "
                "probing that order shape."
            )
        for name, scenario in SCENARIOS.items():
            if name in p.capabilities:
                pairs.append((p, scenario))
    return pairs


def inject_placement_attributes(sdl_text: str, attrs: dict[str, str]) -> str:
    """Inject ``placement.akash.attributes`` so only matching providers may bid.

    Ported from the in-cluster probe. Without it a public probe order attracts
    20+ unrelated providers and ours loses Akash's per-order bid cap race
    ("too many existing bids (20)") — which reads as a NO-BID from a provider
    that was never given the chance.

    Raises rather than warning if the SDL's placement block is not in the
    expected shape: the in-cluster version printed a warning and continued,
    which would submit an unpinned order and produce exactly the false NO-BID
    this function exists to prevent.
    """
    if not attrs:
        raise ValueError("refusing to submit an unpinned probe order: no attributes")
    attrs_yaml = (
        "      attributes:\n" + "\n".join(f"        {k}: {v}" for k, v in attrs.items()) + "\n"
    )
    needle = "  placement:\n    akash:\n      pricing:"
    if needle not in sdl_text:
        raise ValueError(
            "SDL placement block not in the expected shape — cannot pin the order "
            "to a single provider, so the run would produce meaningless NO-BIDs"
        )
    replacement = f"  placement:\n    akash:\n{attrs_yaml}      pricing:"
    return sdl_text.replace(needle, replacement, 1)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

# Terminal verdicts. Only "bid" and "no-bid" are ANSWERS; everything else means
# we failed to ask the question properly and must not be alerted on as a
# provider fault.
OUTCOME_BID = "bid"
OUTCOME_NO_BID = "no-bid"
OUTCOME_INDEX_LAG = "bid-index-lag"
# The chain cross-check could not confirm the absence. NOT the same as
# "the chain says nobody bid" — see the block where this is returned.
OUTCOME_NO_BID_UNVERIFIED = "no-bid-unverified"
OUTCOME_NO_CREDIT = "no-credit"
OUTCOME_ERROR = "probe-error"

# "Couldn't test" is not "failed" (rule 2 in this module's docstring). An
# unverifiable cross-check belongs here for exactly the reason index-lag does:
# the answer is meaningless, so it must not reach the operator as a provider
# verdict. Measured on onidc 2026-08-29: 55 of 55 no-bids in the entire
# 480-record history carried note="chain cross-check unverifiable" and ZERO
# were chain-confirmed — i.e. every critical page this rule has ever produced
# was false.
_SKIPPED_OUTCOMES = frozenset(
    {OUTCOME_INDEX_LAG, OUTCOME_NO_BID_UNVERIFIED, OUTCOME_NO_CREDIT, OUTCOME_ERROR}
)


@dataclass
class ProbeRecord:
    cluster: str
    provider: str
    scenario: str
    outcome: str
    dseq: str | None = None
    price_amount: float | None = None
    price_denom: str | None = None
    waited_s: int = 0
    retried: bool = False
    note: str = ""
    ts: float = 0.0

    @property
    def skipped(self) -> bool:
        return self.outcome in _SKIPPED_OUTCOMES

    @property
    def bid(self) -> bool:
        return self.outcome == OUTCOME_BID

    def as_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "cluster": self.cluster,
            "provider": self.provider,
            "scenario": self.scenario,
            "outcome": self.outcome,
            "dseq": self.dseq,
            "price_amount": self.price_amount,
            "price_denom": self.price_denom,
            "waited_s": self.waited_s,
            "retried": self.retried,
            "note": self.note[:300],
        }


# Deliberately narrow. A credit verdict aborts probing for EVERY remaining pair,
# so a false positive blinds the whole fleet for the run. A bare "402" matched
# anywhere would do that on any message that happens to contain those digits —
# a dseq, a byte count, a timestamp — so the HTTP status must look like a status
# and the prose markers must be unambiguous.
_CREDIT_MARKERS = (
    "insufficient credit",
    "insufficient balance",
    "payment required",
    "http 402",
    "status 402",
    "status_code=402",
    "(402)",
)


def _is_credit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _CREDIT_MARKERS)


def probe_pair(
    client: Any,
    target: ProviderTarget,
    scenario: Scenario,
    *,
    wait_s: int = 45,
    poll_s: int = 5,
    deposit: float = 0.5,
    now: float | None = None,
) -> ProbeRecord:
    """Submit one pinned order, wait for THIS provider's bid, always close it.

    Never raises for an ordinary probe failure — the failure IS the datum. It
    re-raises only a credit error, which the caller must treat as fleet-wide.
    """
    from .capacity import probe_order_sdl

    ts = now if now is not None else time.time()
    sdl = inject_placement_attributes(scenario.sdl, target.attributes)
    try:
        res = probe_order_sdl(
            client,
            sdl,
            provider=target.wallet,
            wait_s=wait_s,
            poll_s=poll_s,
            deposit=deposit,
        )
    except Exception as exc:  # noqa: BLE001 - classified, not swallowed
        if _is_credit_error(exc):
            raise
        return ProbeRecord(
            cluster=target.cluster,
            provider=target.wallet,
            scenario=scenario.name,
            outcome=OUTCOME_ERROR,
            note=f"{type(exc).__name__}: {exc}",
            ts=ts,
        )

    bidders = res.get("bidders") or []
    if bidders:
        b = bidders[0]
        return ProbeRecord(
            cluster=target.cluster,
            provider=target.wallet,
            scenario=scenario.name,
            outcome=OUTCOME_BID,
            dseq=res.get("dseq"),
            price_amount=b.get("price_amount"),
            price_denom=b.get("price_denom"),
            waited_s=int(res.get("waited_s") or 0),
            ts=ts,
        )

    # No bid from us. Before believing it, ask the chain: the Console bid index
    # answering HTTP-200-empty during indexer lag is indistinguishable from
    # "nobody bid" (2026-07-23 audit). An index lag is NOT a provider fault.
    from .smoke_providers import _chain_bids_exist

    on_chain = _chain_bids_exist(str(res.get("dseq") or ""))
    if on_chain is True:
        return ProbeRecord(
            cluster=target.cluster,
            provider=target.wallet,
            scenario=scenario.name,
            outcome=OUTCOME_INDEX_LAG,
            dseq=res.get("dseq"),
            waited_s=int(res.get("waited_s") or 0),
            note="chain reports bids the Console index did not return",
            ts=ts,
        )
    # on_chain is False -> the chain positively confirms nobody bid. That is a
    # real NO-BID and must page.
    # on_chain is None  -> neither the Console index nor the LCDs could answer.
    #                      That is an absence of evidence, not evidence of
    #                      absence, and scoring it as a provider failure is the
    #                      exact mistake this module's docstring forbids. It was
    #                      previously separated only by a note string, which the
    #                      exported metric and therefore the alert cannot see.
    return ProbeRecord(
        cluster=target.cluster,
        provider=target.wallet,
        scenario=scenario.name,
        outcome=OUTCOME_NO_BID if on_chain is False else OUTCOME_NO_BID_UNVERIFIED,
        dseq=res.get("dseq"),
        waited_s=int(res.get("waited_s") or 0),
        note="" if on_chain is False else "chain cross-check unverifiable",
        ts=ts,
    )


def run_probe(
    client: Any,
    *,
    providers: Iterable[ProviderTarget] = PROVIDERS,
    wait_s: int = 45,
    poll_s: int = 5,
    deposit: float = 0.5,
    retry_delay_s: int = 60,
    sleep: Any = time.sleep,
    now: Any = time.time,
) -> list[ProbeRecord]:
    """Probe every eligible pair once, confirming any NO-BID with one retry.

    The retry ports the in-cluster probe's 15-minute confirming re-probe into a
    single run: a lone poll that missed a bid by a second is a flake, and a
    flake that pages critical trains people to ignore the page. A real outage
    survives the retry.

    On a credit error every REMAINING pair is recorded as skipped rather than
    omitted, so a dry grant is visible immediately instead of looking like a
    run that never happened.
    """
    pairs = eligible_pairs(providers)
    records: list[ProbeRecord] = []
    credit_exhausted_note = ""

    for idx, (target, scenario) in enumerate(pairs):
        if credit_exhausted_note:
            records.append(
                ProbeRecord(
                    cluster=target.cluster,
                    provider=target.wallet,
                    scenario=scenario.name,
                    outcome=OUTCOME_NO_CREDIT,
                    note=credit_exhausted_note,
                    ts=now(),
                )
            )
            continue

        try:
            rec = probe_pair(
                client,
                target,
                scenario,
                wait_s=wait_s,
                poll_s=poll_s,
                deposit=deposit,
                now=now(),
            )
        except Exception as exc:  # noqa: BLE001 - credit only, per probe_pair
            credit_exhausted_note = f"{type(exc).__name__}: {exc}"[:300]
            records.append(
                ProbeRecord(
                    cluster=target.cluster,
                    provider=target.wallet,
                    scenario=scenario.name,
                    outcome=OUTCOME_NO_CREDIT,
                    note=credit_exhausted_note,
                    ts=now(),
                )
            )
            continue

        if rec.outcome in (OUTCOME_NO_BID, OUTCOME_NO_BID_UNVERIFIED) and retry_delay_s > 0:
            print(
                f"  {target.cluster}/{scenario.name}: no bid — confirming in {retry_delay_s}s",
                file=sys.stderr,
            )
            sleep(retry_delay_s)
            try:
                confirm = probe_pair(
                    client,
                    target,
                    scenario,
                    wait_s=wait_s,
                    poll_s=poll_s,
                    deposit=deposit,
                    now=now(),
                )
                confirm.retried = True
                rec = confirm
            except Exception as exc:  # noqa: BLE001
                credit_exhausted_note = f"{type(exc).__name__}: {exc}"[:300]
                rec = ProbeRecord(
                    cluster=target.cluster,
                    provider=target.wallet,
                    scenario=scenario.name,
                    outcome=OUTCOME_NO_CREDIT,
                    note=credit_exhausted_note,
                    retried=True,
                    ts=now(),
                )

        records.append(rec)
        print(f"  [{idx + 1}/{len(pairs)}] {target.cluster}/{scenario.name}: {rec.outcome}")

    return records


# ---------------------------------------------------------------------------
# Prometheus rendering
# ---------------------------------------------------------------------------

M_RESULT = "just_akash_bidprobe_result"
M_SKIPPED = "just_akash_bidprobe_skipped"
M_PAIR_TS = "just_akash_bidprobe_pair_timestamp"
M_RUN_TS = "just_akash_bidprobe_run_timestamp"
M_PRICE = "just_akash_bidprobe_bid_price"
M_SKIP_INFO = "just_akash_bidprobe_skip_info"


def _esc(v: str) -> str:
    # \r matters as much as \n: the consumers parse line-by-line and a stray
    # carriage return splits one sample into two malformed ones, which drops the
    # WHOLE document at the allowlist.
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _finite(value: Any) -> float | None:
    """Return ``value`` as a float only if it is a real, finite number.

    ``_extract_bid_price`` falls back to ``float('inf')`` on a malformed bid
    payload, and Prometheus exposition has no representation for that: an
    ``inf`` sample is a parse error, and one bad line makes the consumers drop
    every series in the file — including the no-bid signal this exists to
    carry. A price we cannot render is simply omitted; the alert never reads it.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN or ±inf
        return None
    return f


def render_prom(records: list[ProbeRecord], *, run_ts: float | None = None) -> str:
    """Render the exposition consumed by each cluster's Prometheus.

    Contract, and the reason each line exists:

    * ``result`` 1/0 — did OUR provider bid. Emitted for tested pairs only; a
      skipped pair has no answer and must not assert one, in either direction.
    * ``skipped`` 0/1 — emitted for EVERY eligible pair on EVERY run. This is
      the join factor the per-cluster no-bid rule multiplies by, so its zero
      case is what allows the alert to fire at all.
    * ``pair_timestamp`` — per pair, so a rule can refuse to trust a result
      older than one cadence. Scraping a static file means the last good values
      are re-ingested forever if the producer dies; freshness has to be carried
      IN the data, not inferred from the scrape.
    * ``run_timestamp`` — fleet-level staleness, single owner.
    * ``bid_price`` — absent when there was no bid. Never zero: a zero price
      would be indistinguishable from a free bid.
    """
    ts = run_ts if run_ts is not None else time.time()
    out: list[str] = []
    out.append(f"# HELP {M_RESULT} 1 if our provider bid on this order shape, 0 if it did not")
    out.append(f"# TYPE {M_RESULT} gauge")
    out.append(
        f"# HELP {M_SKIPPED} 1 if the pair could not be tested this run (never a provider fault)"
    )
    out.append(f"# TYPE {M_SKIPPED} gauge")
    out.append(f"# HELP {M_PAIR_TS} Unix time this pair last produced a real answer")
    out.append(f"# TYPE {M_PAIR_TS} gauge")
    out.append(f"# HELP {M_RUN_TS} Unix time of the most recent bid-probe run")
    out.append(f"# TYPE {M_RUN_TS} gauge")
    out.append(f"# HELP {M_PRICE} Bid price our provider offered, in the bid's denom")
    out.append(f"# TYPE {M_PRICE} gauge")
    out.append(f"# HELP {M_SKIP_INFO} Reason a pair was untestable this run")
    out.append(f"# TYPE {M_SKIP_INFO} gauge")

    for r in sorted(records, key=lambda x: (x.cluster, x.scenario)):
        lbl = (
            f'cluster="{_esc(r.cluster)}",provider="{_esc(r.provider)}"'
            f',scenario="{_esc(r.scenario)}"'
        )
        out.append(f"{M_SKIPPED}{{{lbl}}} {1 if r.skipped else 0}")
        if r.skipped:
            out.append(f'{M_SKIP_INFO}{{{lbl},reason="{_esc(r.outcome)}"}} 1')
            continue
        out.append(f"{M_RESULT}{{{lbl}}} {1 if r.bid else 0}")
        out.append(f"{M_PAIR_TS}{{{lbl}}} {r.ts:.0f}")
        price = _finite(r.price_amount) if r.bid else None
        if price is not None:
            denom = _esc(r.price_denom or "")
            out.append(f'{M_PRICE}{{{lbl},denom="{denom}"}} {price}')

    out.append(f"{M_RUN_TS} {ts:.0f}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe whether our providers still bid.")
    ap.add_argument(
        "--cluster",
        action="append",
        default=[],
        help="limit to these clusters (repeatable); default all",
    )
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait for a bid")
    ap.add_argument("--deposit", type=float, default=0.5, help="order deposit (ACT)")
    ap.add_argument(
        "--retry-delay",
        type=int,
        default=60,
        help="seconds before confirming a NO-BID; 0 disables the retry",
    )
    ap.add_argument("--prom-out", default="", help="write the exposition here")
    ap.add_argument("--jsonl-out", default="", help="append raw records here")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the eligible pairs and exit without spending anything",
    )
    args = ap.parse_args(argv)

    targets = [p for p in PROVIDERS if not args.cluster or p.cluster in args.cluster]
    if not targets:
        print(f"ERROR: no provider matches --cluster {args.cluster}", file=sys.stderr)
        return 1

    if args.dry_run:
        for t, s in eligible_pairs(targets):
            print(f"{t.cluster:12} {s.name:18} {t.wallet}")
        return 0

    api_key = os.environ.get("AKASH_API_KEY", "").strip()
    if not api_key:
        print("ERROR: AKASH_API_KEY is not set", file=sys.stderr)
        return 1

    from .api import AkashConsoleAPI

    client = AkashConsoleAPI(api_key)
    started = time.time()
    records = run_probe(
        client,
        providers=targets,
        wait_s=args.wait,
        deposit=args.deposit,
        retry_delay_s=args.retry_delay,
        sleep=(lambda _s: None) if args.retry_delay <= 0 else time.sleep,
    )

    # A SCOPED run must not publish an exposition. The published .prom is
    # overwritten wholesale each run, so a one-cluster run would delete every
    # other cluster's series — and each of those clusters' Prometheus would
    # watch its own metrics vanish because somebody was debugging a third one.
    # Measured on 2026-08-13: three consecutive scoped dispatches left the
    # published file describing alphavps alone.
    #
    # The JSONL is still appended: it is the audit trail and is append-only, so
    # a scoped run adds to it without erasing anything.
    if args.prom_out and args.cluster:
        print(
            f"NOT writing {args.prom_out}: this run covered only "
            f"{sorted(set(args.cluster))}, and publishing it would drop every "
            "other cluster's series. Run without --cluster to publish.",
            file=sys.stderr,
        )
    elif args.prom_out:
        with open(args.prom_out, "w", encoding="utf-8") as fh:
            fh.write(render_prom(records, run_ts=started))
        print(f"wrote {args.prom_out}")
    if args.jsonl_out:
        with open(args.jsonl_out, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r.as_json()) + "\n")
        print(f"appended {len(records)} records to {args.jsonl_out}")

    tested = [r for r in records if not r.skipped]
    no_bid = [r for r in tested if not r.bid]
    print(
        f"\n{len(records)} pairs: {len(tested)} tested, "
        f"{len(records) - len(tested)} skipped, {len(no_bid)} NO-BID"
    )
    for r in no_bid:
        print(f"  NO-BID {r.cluster}/{r.scenario} (dseq={r.dseq})")

    # Exit 0 even on NO-BID: the alerting path is the metrics, not CI status.
    # A red CI run for a provider outage trains people to ignore red CI runs,
    # and the same mistake at daily cadence kept a warning pinned for 26h
    # (onidc `update`, 2026-07-21). Only a broken PROBE is a failed run.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
