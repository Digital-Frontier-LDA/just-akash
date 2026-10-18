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

import hashlib
import re
import textwrap
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
# ⛔ THE KEY MAY BE A SHELL TEMPLATE, AND AN UNREADABLE KEY MUST NOT READ AS NO KEY.
# `runner-pool.yml` renders its SDL from a heredoc and takes the placement key from its
# `placement-key` input, so the text carries `${PLACEMENT_KEY}:`. The original character
# class had no `$`/`{`/`}`, so the scanner returned an EMPTY list for that document —
# and empty is the one answer a caller cannot act on safely: "every key starts with our
# prefix" is vacuously TRUE of no keys. Reporting the template lets the caller decide
# what to do with it (test_provenance checks the input's default instead), which is the
# same principle as the block-scalar handling below: a guard that silently skips what it
# cannot read is not a guard.
# The shell-template shape, named once. `_KEY_RE` matches it as one alternative and
# `is_templated` asks about it directly; two spellings of the same idea is how a caller
# comes to disagree with the scanner about what it just accepted.
_TEMPLATE = r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"
_TEMPLATE_RE = re.compile(_TEMPLATE)
_KEY_RE = re.compile(
    r"^(?P<ind>\s*)"
    r"(?P<key>"
    + _TEMPLATE  # a shell template: ${PLACEMENT_KEY}
    + r"|[A-Za-z0-9._-]+"  # or a literal key
    r")"
    r":\s*$"
)
# A block scalar opener: `key: |`, `- >-`, `args: |2` etc. Everything indented under one is
# STRING CONTENT, not YAML structure.
_BLOCK_OPEN_RE = re.compile(r":\s*[|>][0-9+-]*\s*$|^\s*-\s*[|>][0-9+-]*\s*$")


def is_templated(key: str) -> bool:
    """True if `key` is a shell template rather than a literal placement key.

    ⛔ EXPORTED SO THERE IS ONE DEFINITION. A caller deciding "is this a template?" needs
    the same answer `_KEY_RE` gave when it accepted the key; a second regex written beside
    it drifts, and the drift is silent — a template the caller does not recognise falls
    through to the literal branch and gets judged as an unstamped key. Raised on the PR
    that introduced the template form, where a test's private copy accepted only
    `[A-Z_]+` and would have misjudged `${PLACEMENT_KEY2}`.
    """
    return bool(_TEMPLATE_RE.fullmatch(key))


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


# Separates the workload part of a key from the per-run part: `just-akash-runner.a1b2c3`.
# A DOT, not a hyphen: workload names already contain hyphens (`just-akash-runner`), so a
# hyphen would make "where does the run id start" ambiguous for anything reading the key
# back. `_KEY_RE` already admits dots, and so does the on-chain name.
RUN_SEPARATOR = "."
_RUN_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")
# A whole key line, with any trailing comment captured so it can be kept.
_SCOPE_LINE_RE = re.compile(
    r"^(?P<ind>[ \t]+)(?P<key>[A-Za-z0-9._-]+):(?P<rest>[ \t]*(?:#[^\n]*)?)$"
)


def run_scoped(key: str, run_id: str) -> str:
    """`just-akash-runner` + `a1b2c3` -> `just-akash-runner.a1b2c3`.

    PREFIX MATCHING IS UNAFFECTED, which is what makes this safe to add after the fact:
    the sibling sweeper, this module's guard and cleanup_stale all use ``startswith``, so
    a suffix changes nothing they rely on. Nothing in this repo compares a placement key
    for equality — verified — and ``canary/ensure.py`` matches SERVICE names, so a key
    that changes per run cannot trigger the redeploy loop #145 exists to prevent.
    """
    if not run_id or not key:
        return key
    return f"{key}{RUN_SEPARATOR}{run_id}"


def run_id_of(key: str) -> str:
    """The per-run part of a placement key, or "" when it carries none.

    Returns "" rather than guessing for keys minted before run-scoping existed, so an
    older deployment reads as "repo-attributable, not run-attributable" — which is
    exactly what it is.
    """
    head, sep, tail = key.rpartition(RUN_SEPARATOR)
    if not sep or not head.startswith(PLACEMENT_PREFIX):
        return ""
    return tail if _RUN_ID_RE.match(tail) else ""


def _rewrite_keys(sdl_text: str, targets: dict[str, str]) -> str:
    """Rename placement keys per ``targets``, at every structural occurrence."""
    # A LINE WALK, not a regex over the document, and for the same reason
    # `placement_keys` is one: an SDL may embed a whole YAML document inside a block
    # scalar, e.g. `args: - |` followed by indented text a container receives as an
    # argument. That text is STRING CONTENT. A regex anchored to `^<indent><key>:`
    # matches it perfectly well and would rewrite what a container is passed — silently
    # changing the workload rather than the placement.
    lines = sdl_text.split("\n")
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Copy a block scalar's body through untouched.
        if stripped and _BLOCK_OPEN_RE.search(line):
            out_lines.append(line)
            body_min = indent + 1
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) < body_min:
                    break
                out_lines.append(nxt)
                i += 1
            continue

        m = _SCOPE_LINE_RE.match(line)
        if m and m.group("key") in targets:
            # The trailing comment is preserved. The key appears twice, and a comment on
            # only ONE of them — typically the `deployment.<service>.<KEY>` reference,
            # where "# picks the placement above" is a natural note — would otherwise
            # leave that reference pointing at a placement group that no longer exists.
            # Akash rejects that SDL, so a comment in a caller's file would break the
            # deploy outright.
            out_lines.append(f"{m.group('ind')}{targets[m.group('key')]}:{m.group('rest')}")
        else:
            out_lines.append(line)
        i += 1

    return "\n".join(out_lines)


def stamp_run(sdl_text: str, run_id: str) -> tuple[str, list[str]]:
    """Rewrite every OWNED placement key to carry ``run_id``. Returns (sdl, new keys).

    Only keys already carrying this repo's prefix are touched. A caller may pass their
    own SDL declaring any placement key, and rewriting that would claim provenance over
    a document we do not control — the deployment is ours to account for either way, but
    the marker should say what it can prove, and `just-akash-` on someone else's SDL
    would assert authorship we did not have.

    The key appears TWICE and both must move together: `profiles.placement.<KEY>` and the
    `deployment.<service>.<KEY>` reference that selects it. Rewriting one leaves an SDL
    that names a placement group which does not exist, which Akash rejects — so this
    rewrites by exact key line and returns the new keys for the caller to verify.
    """
    if not run_id:
        return sdl_text, []
    targets = {
        key: run_scoped(key, run_id)
        for key in placement_keys(sdl_text)
        if key.startswith(PLACEMENT_PREFIX) and not run_id_of(key)
    }
    if not targets:
        return sdl_text, []

    return _rewrite_keys(sdl_text, targets), list(targets.values())


# ── Lease charters (openmix-pvr6.1 / just-akash-3sq) ─────────────────────────────────────
# Flynn's attribution of OUR leases, by placement name (openmix app/background/leases.py,
# _NAME_PREFIXES / _NAME_EXACT, classes in OURS_CLASSES). Mirrored, not imported: Flynn is
# another repo, and test_provenance re-checks each stamped name against a copy of its rules.
OURS_PREFIXES = ("flynn-", "fleet-")
OURS_EXACT = frozenset({"dcloud", "akash"})
CHARTER_ID_RE = re.compile(r"ch-[0-9a-f]{10}")
CHARTER_SEPARATOR = "."
# The longest placement name verified on mainnet (see the module docstring).
MAX_PLACEMENT_KEY_LEN = 62
# Hex characters of the key's hash that replace the cut end of an over-long key.
_CUT_HASH_LEN = 6


def uncharted_key(key: str) -> str:
    """The placement key without its ``.ch-<id>`` charter suffix, if it has one."""
    head, sep, tail = key.rpartition(CHARTER_SEPARATOR)
    return head if sep and CHARTER_ID_RE.fullmatch(tail) else key


def is_ours_placement(key: str) -> bool:
    """True when Flynn counts a lease with this placement name as OURS: ``flynn-*``,
    ``fleet-*``, or exactly ``dcloud`` / ``akash``. A charter suffix does not change it."""
    base = uncharted_key(key)
    return base.startswith(OURS_PREFIXES) or base in OURS_EXACT


def stamp_charter(sdl_text: str, charter_id: str) -> tuple[str, list[str]]:
    """Append ``.<charter_id>`` to every ``flynn-``/``fleet-`` placement key. Returns
    (sdl, new keys), like stamp_run, and by the same line walk: both copies of the key move,
    block scalars and trailing comments are untouched.

    ⛔ THE PREFIX IS WHAT FLYNN ATTRIBUTES ON, so it is never cut. A key that would pass
    62 characters loses the END of its workload part instead, and that end is replaced by
    a short hash of the whole key: two keys sharing their first 48 characters must not
    collapse into ONE placement group (a duplicate YAML key — one group silently dropped
    while the lease is still funded). Any collision that remains raises, before the spend.

    ⛔ ``dcloud`` and ``akash`` ARE NOT STAMPED. Flynn matches them as EXACT names, so
    ``dcloud.ch-…`` would read as UNATTRIBUTED — the opposite of what a charter is for.
    Their charter reaches Flynn through the bind alone.

    A key already carrying a DIFFERENT charter id (an SDL reused from an earlier chartered
    deploy) is re-stamped with this one: the placement name must point at the charter that
    pays for THIS lease. The same id is left alone, so stamping twice is a no-op.
    """
    if not CHARTER_ID_RE.fullmatch(charter_id or ""):
        raise ValueError(f"not a charter id: {charter_id!r}")
    suffix = f"{CHARTER_SEPARATOR}{charter_id}"
    room = MAX_PLACEMENT_KEY_LEN - len(suffix)
    existing = placement_keys(sdl_text)
    targets: dict[str, str] = {}
    for key in existing:
        base = uncharted_key(key)
        if not base.startswith(OURS_PREFIXES) or key.endswith(suffix):
            continue
        prefix = next(p for p in OURS_PREFIXES if base.startswith(p))
        if len(base) > room:
            digest = hashlib.sha256(base.encode()).hexdigest()[:_CUT_HASH_LEN]
            base = f"{base[: max(room - _CUT_HASH_LEN - 1, len(prefix))]}-{digest}"
        targets[key] = base + suffix
    if not targets:
        return sdl_text, []
    new_keys = list(targets.values())
    untouched = set(existing) - set(targets)
    clashes = sorted({k for k in new_keys if new_keys.count(k) > 1} | (set(new_keys) & untouched))
    if clashes:
        raise ValueError(
            f"charter stamping would merge placement groups into {', '.join(clashes)}; "
            "rename the placement keys so they differ within their first "
            f"{room - _CUT_HASH_LEN - 1} characters"
        )

    return _rewrite_keys(sdl_text, targets), list(targets.values())


def sdl_files() -> list[Path]:
    return sorted(p for p in _SDL_DIR.glob("*.yaml") if p.is_file())


_WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github/workflows"
# `cat > <path> <<WORD` / `<<-WORD`, optionally quoted. The indent is captured because the
# terminator must match it — see _heredocs.
_HEREDOC_RE = re.compile(
    r"^(?P<indent>[ \t]*)cat\s*>\s*(?P<path>\S+)\s*<<(?P<dash>-?)\s*"
    r"(?P<q>['\"]?)(?P<word>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[ \t]*$"
)


def _heredocs(text: str) -> list[tuple[str, str]]:
    """(target path, dedented body) for every heredoc in one shell-bearing document.

    Terminator matching follows the SHELL, not convenience. `<<WORD` ends only at a line
    that is exactly WORD — no leading and no trailing whitespace — and `<<-WORD` allows
    leading TABS only. Accepting `strip() == WORD` was more permissive than bash in two
    ways that both matter here:

      * a body line that merely trims to the marker would cut the SDL short, and
      * `WORD` with trailing spaces would end a heredoc the shell leaves open.

    Either way the workflow's real behaviour and this scanner's reading diverge, and the
    scanner is the half that decides whether the provenance guard passes. A guard that
    green-lights a workflow whose heredoc semantics are broken is worse than no guard.

    The indent is the opener's, because these bodies are read from RAW workflow YAML where
    the block scalar's indentation is still present; the YAML parser strips it uniformly
    before the shell ever sees it, so the opener and terminator share it.

    EOF before the terminator yields NOTHING. Bash does not treat that as a completed
    heredoc, so neither may this — otherwise a truncated workflow produces a
    valid-looking SDL and the guard passes on a file that would not run.
    """
    out: list[tuple[str, str]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _HEREDOC_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent, dash, word = m.group("indent"), m.group("dash"), m.group("word")
        body: list[str] = []
        i += 1
        closed = False
        while i < len(lines):
            line = lines[i]
            rest = line[len(indent) :] if line.startswith(indent) else None
            if rest is not None and (rest.lstrip("\t") == word if dash else rest == word):
                closed = True
                i += 1
                break
            body.append(line)
            i += 1
        if closed:
            out.append((m.group("path"), textwrap.dedent("\n".join(body))))
    return out


def inline_sdls() -> list[tuple[str, str]]:
    """(label, dedented text) for every SDL a WORKFLOW renders inline.

    Not every SDL this repo deploys lives in ``sdl/``. ``runner-pool.yml`` writes its own
    with a heredoc, because the pool's shape depends on caller inputs. A prefix guard that
    globs ``sdl/*.yaml`` therefore could not see the one SDL that is deployed most often
    and lives longest — and an unstamped runner pool is exactly what the sibling sweeper
    closes: non-GPU, and with ``ephemeral: false`` legitimately older than 12h. The
    symptom would be runners vanishing mid-job and reported as RUNNER_NEVER_REGISTERED
    against a provider that did nothing wrong.

    This module already refuses to silently skip files it cannot read. An SDL it never
    looked at is the same failure, one directory over.
    """
    out: list[tuple[str, str]] = []
    if not _WORKFLOW_DIR.is_dir():
        return out
    for wf in sorted(_WORKFLOW_DIR.glob("*.y*ml")):
        for path, text in _heredocs(wf.read_text(encoding="utf-8")):
            # Only heredocs that ARE an SDL — a workflow writes plenty of other files.
            if re.search(r"^services:\s*$", text, re.M) and re.search(
                r"^profiles:\s*$", text, re.M
            ):
                out.append((f"{wf.name}:{path}", text))
    return out
