"""On-chain provenance marker for every Akash deployment this repo creates.

WHY THIS EXISTS — WE WERE BEING DELETED
---------------------------------------
Our Console wallet is SHARED with sibling repos, and one of them
(``Borduas-Holdings/Blazing-Back``) runs a leak sweeper on it every 3 hours:
``scripts/cleanup_leaked_akash_deployments.py``, cron ``30 */3 * * *``. It closes any
NON-GPU deployment older than ``MAX_AGE_HOURS`` (12) as a presumed interrupted-CI leak.

The per-provider canary is non-GPU and PERSISTENT BY DESIGN, so it matched that filter
every time. Measured on chain 2026-08-11/12, five closures, every one
``MsgCloseDeployment`` signed by our own wallet:

    lease                created            closed             lived
    hetzner_hel          08-11 04:35:14Z    08-11 18:49:02Z    14.230 h
    hetzner_hel          08-11 18:53:29Z    08-12 06:57:14Z    12.063 h
    onidc                08-11 00:54:19Z    08-11 12:48:58Z    11.911 h

Each closure lands 18-27 minutes after a ``30 */3`` sweep slot — the sweep enumerating
and issuing DELETEs. The 12h threshold quantised by a 3h cron is exactly the 11.9-14.2h
spread we observed and briefly mistook for a "fixed ~13.7h timer".

The cost was not only ours. df-grafana paged CRITICAL against alphavps, hetzner_hel and
onidc — "this provider does not keep customer deployments alive" — for deployments no
provider ever touched. And the same sweeper destroyed a sibling's research deployment
four times, taking a 200 GiB persistent volume with it each time.

WHY A PLACEMENT KEY, AND NOT ANYTHING ELSE
------------------------------------------
``group_spec.name`` IS the SDL ``profiles.placement.<KEY>`` key. It is author-controlled,
written ATOMICALLY inside ``MsgCreateDeployment`` (so no untagged window exists), immutable
afterwards, and readable from public chain REST with ZERO auth. The sweeper's own module
(``control-plane/api/core/akash_provenance.py``) records that a 62-character key was
verified on mainnet: 8 bids, lease created, ``state=active``, read back byte-exact.

⚠ Do NOT move this marker to ``placement.attributes``. Attributes are matched against
provider CAPABILITIES during bidding, so an attribute nobody advertises yields ZERO BIDS.
``group_spec.name`` is never consulted by the bid engine.

⚠ Do NOT reuse the sibling's prefix (``dfci-infra-``). That prefix is what the sweeper
REAPS. Its module states the rule plainly: "Any repo sharing these wallets must pick its
own distinct prefix." Ours is deliberately different, so their sweeper protects our
deployments instead of destroying them.

⚠ The placement key is NOT the service name and NOT the profile name. Renaming a service
changes the lease's advertised service set, which ``canary/ensure.py`` plan() matches on
exactly (``{"canary"}``) — so renaming it would make every canary unrecognisable and
trigger the redeploy loop that #145 exists to prevent. Only the ``placement`` key and its
reference under ``deployment.<service>.<KEY>`` carry this marker.
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo-scoped. Everything this repo puts on the shared wallet carries it.
# ⚠ Changing this orphans every deployment already stamped with the old value: they stop
# matching, which under a positive-allowlist sweeper means PROTECTED and alarmed, never
# silently destroyed. Still — change it deliberately and drain first.
PLACEMENT_PREFIX = "just-akash-"

# The sibling repo's prefix. Listed so the test can assert we never collide with it: a
# deployment of ours carrying THEIR prefix would be reaped by their sweeper on schedule.
SIBLING_REAPED_PREFIX = "dfci-infra-"

_SDL_DIR = Path(__file__).resolve().parent.parent / "sdl"

# `placement:` and the key one level beneath it. Deliberately a line scanner rather than a
# YAML parse: sdl/github-runner-probe.yaml is templated and does not survive safe_load, and
# a guard that silently skips the files it cannot read is not a guard.
_PROFILES_RE = re.compile(r"^profiles:\s*$")
_PLACEMENT_RE = re.compile(r"^(?P<ind>\s{2})placement:\s*$")
_KEY_RE = re.compile(r"^(?P<ind>\s*)(?P<key>[A-Za-z0-9._-]+):\s*$")
# A block scalar opener: `key: |`, `- >-`, `args: |2` etc. Everything indented under one is
# STRING CONTENT, not YAML structure.
_BLOCK_OPEN_RE = re.compile(r":\s*[|>][0-9+-]*\s*$|^\s*-\s*[|>][0-9+-]*\s*$")


def placement_keys(text: str) -> list[str]:
    """Every `profiles.placement.<KEY>` key in one SDL document.

    STRUCTURE-AWARE, and it has to be. A bare `placement:` match anywhere would pick up
    text that merely LOOKS like YAML — an SDL may embed a whole document inside a block
    scalar, e.g.::

        services:
          app:
            args:
              - |
                placement:
                  just-akash-fake:

    That is string content passed to a container, not a placement key, and treating it as
    one would have this guard assert against a key that does not exist on chain. So:
    `placement:` counts only at indent 2 directly under a top-level `profiles:`, and every
    block-scalar body is skipped entirely. Raised by CodeRabbit on #150.

    Still a line scanner rather than yaml.safe_load: sdl/github-runner-probe.yaml is
    templated and does not parse, and a guard that silently skips the files it cannot read
    is not a guard.
    """
    keys: list[str] = []
    lines = text.split("\n")
    in_profiles = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Skip the body of any block scalar — it is string content, not structure.
        if stripped and _BLOCK_OPEN_RE.search(line):
            body_min = indent + 1
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) < body_min:
                    break
                i += 1
            continue

        if _PROFILES_RE.match(line):
            in_profiles = True
            i += 1
            continue
        # Any other top-level key ends the profiles block.
        if stripped and indent == 0 and not _PROFILES_RE.match(line):
            in_profiles = False

        if in_profiles and _PLACEMENT_RE.match(line):
            want = 4  # keys sit one level under `  placement:`
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt.strip():
                    j += 1
                    continue
                ind = len(nxt) - len(nxt.lstrip())
                if ind < want:
                    break
                if ind == want:
                    km = _KEY_RE.match(nxt)
                    if km:
                        keys.append(km.group("key"))
                j += 1
            i = j
            continue
        i += 1
    return keys


def sdl_files() -> list[Path]:
    return sorted(p for p in _SDL_DIR.glob("*.yaml") if p.is_file())
