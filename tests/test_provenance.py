"""Every SDL must stamp this repo's provenance prefix, or the sweeper deletes it.

WHAT THIS GUARDS. Our Console wallet is shared with Blazing-Back, whose leak sweeper runs
every 3 hours and closes any non-GPU deployment older than 12 hours. Five canary leases
were destroyed that way on 2026-08-11/12 — measured on chain, every closure a
MsgCloseDeployment from our own wallet — and df-grafana paged three innocent providers for
it. An SDL added later without the prefix is not a style problem; it is a deployment that
gets deleted every ~12 hours and a metric that lies about whose fault it was.
"""

from __future__ import annotations

import re
from pathlib import Path

from just_akash.provenance import (
    PLACEMENT_PREFIX,
    SIBLING_REAPED_PREFIX,
    _heredocs,
    inline_sdls,
    is_templated,
    placement_keys,
    run_id_of,
    run_scoped,
    sdl_files,
    stamp_run,
)


def test_there_are_sdls_to_check():
    """A guard that silently checks nothing is worse than no guard.

    If sdl/ moves or the glob stops matching, every assertion below passes vacuously and
    the next SDL ships unprotected. This repo has been bitten by exactly that shape twice
    (check_allowlist_ratchet.py, verify_rules_loaded.py), so assert the subject exists.
    """
    files = sdl_files()
    assert len(files) >= 4, [str(f) for f in files]


def test_every_sdl_stamps_the_repo_prefix():
    for path in sdl_files():
        keys = placement_keys(path.read_text(encoding="utf-8"))
        assert keys, f"{path.name}: no placement key found — is the file still an SDL?"
        for key in keys:
            assert key.startswith(PLACEMENT_PREFIX), (
                f"{path.name}: placement key {key!r} does not start with "
                f"{PLACEMENT_PREFIX!r}. Unstamped deployments are indistinguishable from a "
                f"CI leak on the shared wallet and are closed after 12h."
            )


# --------------------------------------------------------------------------
# Not every SDL lives in sdl/ — the one deployed most often is rendered inline
# --------------------------------------------------------------------------


def test_the_inline_workflow_sdls_are_actually_found():
    """Same anti-vacuity rule as `test_there_are_sdls_to_check`, one directory over.

    `runner-pool.yml` renders its SDL with a heredoc rather than shipping a file, because
    the pool's shape depends on caller inputs. So a guard globbing `sdl/*.yaml` could not
    see the SDL this repo deploys most often — and if the extractor ever stops matching,
    every assertion below passes while checking nothing."""
    found = inline_sdls()
    assert found, "no inline SDL extracted — the heredoc scanner has stopped matching"
    assert any("runner-pool" in label for label, _ in found), [lbl for lbl, _ in found]


# A placement key rendered from a caller input rather than written literally. The pool's
# key became `${PLACEMENT_KEY}` so a consumer adopting the canonical escrow reaper can
# namespace its own deployments; see runner-pool.yml's `placement-key` input.
#
# ⛔ ASKED OF THE MODULE, NOT RE-TYPED HERE. This was a private `^\$\{[A-Z_]+\}$`, stricter
# than what `_KEY_RE` accepts — so `${PLACEMENT_KEY2}` would have been SCANNED as a
# template and then JUDGED as a literal, failing for not starting with the repo prefix.
# A guard that disagrees with the scanner it guards is worse than no guard.


def _pool_input_default(name: str) -> str:
    """The default of one `runner-pool.yml` workflow_call input.

    Read from the workflow rather than passed in, so this guard and the pool's own tests
    cannot disagree about what a caller who sets nothing actually deploys.
    """
    import yaml

    wf = Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml"
    doc = yaml.safe_load(wf.read_text())
    call = (doc.get("on") or doc.get(True))["workflow_call"]
    return str(call["inputs"][name]["default"])


def test_every_inline_sdl_stamps_the_repo_prefix():
    """An unstamped runner pool is precisely what the sibling sweeper closes: non-GPU,
    and with `ephemeral: false` legitimately older than 12h. The symptom is runners
    vanishing mid-job, read back as RUNNER_NEVER_REGISTERED against a provider that did
    nothing wrong — a fabricated provider fault, which is the failure this whole runner
    stack exists to stop manufacturing.

    ⛔ ONE KEY IS NOW RENDERED FROM AN INPUT, AND THIS IS NOT A WEAKENING. The claim is
    unchanged — every deployment this repo creates carries an OWNED marker — but for the
    pool the value moved out of the SDL text, so asserting the text would assert the
    wrong object. A templated key is accepted only against the two halves that now carry
    the property, each with its own test in test_runner_pool_workflow.py:

        the DEFAULT is stamped   test_the_placement_key_is_optional_and_defaults_...
        foreign values REFUSED   test_the_guard_actually_runs_and_decides (executed)

    A literal key still has to be stamped here, so this cannot be sidestepped by writing
    one — and a template that is not backed by both halves is a hole, which is why the
    default is re-checked below rather than taken on trust.
    """
    for label, text in inline_sdls():
        keys = placement_keys(text)
        assert keys, f"{label}: no placement key found in the rendered SDL"
        for key in keys:
            if is_templated(key):
                default = _pool_input_default("placement-key")
                assert default.startswith(PLACEMENT_PREFIX), (
                    f"{label}: placement key {key!r} is rendered from an input whose "
                    f"default is {default!r}, which does not start with {PLACEMENT_PREFIX!r}. "
                    f"A caller that sets nothing would deploy unstamped."
                )
                continue
            assert key.startswith(PLACEMENT_PREFIX), (
                f"{label}: placement key {key!r} does not start with {PLACEMENT_PREFIX!r}. "
                f"Unstamped deployments are indistinguishable from a CI leak on the "
                f"shared wallet and are closed after 12h."
            )


def test_no_inline_sdl_wears_the_siblings_reaped_prefix():
    """⚠ A TEMPLATED KEY PASSES THIS TRIVIALLY — `${PLACEMENT_KEY}` never starts with
    `dfci-infra-` — so for the templated case the real claim is that the workflow REFUSES
    the sibling's prefix at run time. That is asserted where it can be executed:
    test_the_guard_refuses_the_sibling_prefix_the_module_names, and behaviourally in
    test_the_guard_actually_runs_and_decides. This function keeps the literal case."""
    for label, text in inline_sdls():
        for key in placement_keys(text):
            assert not key.startswith(SIBLING_REAPED_PREFIX), (
                f"{label}: placement key {key!r} carries the sibling repo's prefix, "
                f"which its leak sweeper closes on a 3-hourly cron."
            )


def test_the_heredoc_extractor_only_returns_sdls():
    """A workflow writes plenty of files with heredocs. Returning a non-SDL would make
    `test_every_inline_sdl_stamps_the_repo_prefix` fail on something that never reaches
    Akash, and the cheapest way to green would be to weaken the guard."""
    for label, text in inline_sdls():
        assert "services:" in text and "profiles:" in text, label


# --- heredoc termination follows the shell, because the guard's answer depends on it ---

_SDL_BODY = "services:\n  a:\n    image: x\nprofiles:\n  placement:\n    just-akash-a:\n"


def _wrap(opener: str, terminator: str) -> str:
    return f"{opener}\n{_SDL_BODY}{terminator}\ntrailing: line\n"


def test_a_plain_terminator_closes_the_heredoc():
    assert len(_heredocs(_wrap("cat > /tmp/x.yaml <<SDL", "SDL"))) == 1


def test_a_trailing_space_does_not_terminate_a_plain_heredoc():
    """bash ends `<<WORD` only at a line that is exactly WORD. Accepting `SDL   ` would
    close a heredoc the shell leaves open — the workflow's real behaviour and this
    scanner's reading would then disagree, and the scanner is the half that decides
    whether the provenance guard passes."""
    assert _heredocs(_wrap("cat > /tmp/x.yaml <<SDL", "SDL   ")) == []


def test_an_over_indented_terminator_does_not_close_it():
    """The terminator must sit at the opener's indent. These bodies are read from RAW
    workflow YAML, where the block scalar's indent is still present and uniform."""
    assert _heredocs(_wrap("  cat > /tmp/x.yaml <<SDL", "      SDL")) == []


def test_the_terminator_must_match_the_openers_indent():
    body = "  cat > /tmp/x.yaml <<SDL\n" + _SDL_BODY + "  SDL\n"
    assert len(_heredocs(body)) == 1


def test_eof_before_the_terminator_yields_nothing():
    """bash does not treat EOF as a completed heredoc, so neither may this. Otherwise a
    truncated workflow produces a valid-looking SDL and the guard passes on a file that
    would never run."""
    assert _heredocs("cat > /tmp/x.yaml <<SDL\n" + _SDL_BODY) == []


def test_a_body_line_that_merely_trims_to_the_marker_does_not_cut_it_short():
    """The old `strip() == word` test would end the heredoc here and silently truncate
    the SDL — losing whatever came after, including the placement block."""
    body = "cat > /tmp/x.yaml <<SDL\n  SDL\n" + _SDL_BODY + "SDL\n"
    found = _heredocs(body)
    assert len(found) == 1
    assert "just-akash-a" in found[0][1], "the SDL was truncated at a look-alike line"


def test_dash_heredoc_allows_leading_tabs_only():
    """`<<-WORD` strips leading TABS, which is the one case where extra whitespace is
    legitimate."""
    assert len(_heredocs("cat > /tmp/x.yaml <<-SDL\n" + _SDL_BODY + "\t\tSDL\n")) == 1
    assert _heredocs("cat > /tmp/x.yaml <<-SDL\n" + _SDL_BODY + "  SDL\n") == []


def test_no_sdl_wears_the_siblings_reaped_prefix():
    """Their prefix is what their sweeper REAPS. Wearing it is opting into deletion."""
    for path in sdl_files():
        for key in placement_keys(path.read_text(encoding="utf-8")):
            assert not key.startswith(SIBLING_REAPED_PREFIX), (
                f"{path.name}: placement key {key!r} carries the sibling repo's prefix, "
                f"which its leak sweeper closes on a 3-hourly cron."
            )


def test_placement_keys_are_distinct_per_workload():
    """The suffix is observability: it is what a human reads in the sweeper's alarm.

    Duplicates are rejected WITHIN one SDL as well as across them — the scanner returns a
    list precisely because a document may declare several, and two groups sharing a name is
    both invalid on chain (unique-within-deployment) and useless as a label. The first
    version only compared across files. Raised by CodeRabbit on #150.
    """
    seen: dict[str, str] = {}
    for path in sdl_files():
        keys = placement_keys(path.read_text(encoding="utf-8"))
        assert len(keys) == len(set(keys)), f"{path.name}: duplicate placement keys {keys}"
        for key in keys:
            assert key not in seen, f"{key!r} is used by both {seen[key]} and {path.name}"
            seen[key] = path.name


# The exact service set each SDL must advertise. canary/ensure.py plan() identifies a canary
# by the lease's service set being EXACTLY {"canary"}, so a rename there is not cosmetic — it
# makes every canary unrecognisable and re-opens the redeploy loop #145 closed.
EXPECTED_SERVICES = {
    "canary.yaml": {"canary"},
    "cpu-backtest-ssh.yaml": {"backtest"},
    "akash-node.yaml": {"node"},
    "github-runner-probe.yaml": {"probe"},
}


def _services(text: str) -> set[str]:
    out: set[str] = set()
    in_services = False
    for line in text.split("\n"):
        if line.startswith("services:"):
            in_services = True
            continue
        if in_services and line and not line[0].isspace():
            break
        if in_services and len(line) - len(line.lstrip()) == 2 and line.strip().endswith(":"):
            out.add(line.strip().rstrip(":"))
    return out


def test_service_sets_are_exactly_what_ensure_py_matches_on():
    """Pins the SET, not just 'it differs from the placement key'.

    The first version of this test only asserted the placement key was not a service name,
    so renaming `canary` to anything else still passed — while its own docstring claimed to
    protect the exact {"canary"} set that plan() matches. A guard that cannot fail for the
    reason it names is the defect this repo keeps finding. Raised by CodeRabbit on #150.
    """
    for path in sdl_files():
        expected = EXPECTED_SERVICES.get(path.name)
        assert expected is not None, (
            f"{path.name} has no entry in EXPECTED_SERVICES. A new SDL must declare its "
            f"service set here deliberately — silence would let a rename through."
        )
        assert _services(path.read_text(encoding="utf-8")) == expected, path.name


def test_placement_key_is_not_the_service_name():
    """Renaming a service changes the lease's advertised service set, and
    canary/ensure.py plan() matches the canary on that set EXACTLY ({"canary"}). A rename
    would make every canary unrecognisable and re-open the redeploy loop #145 closed.
    The first draft of this change did exactly that to sdl/github-runner-probe.yaml, where
    'probe' was simultaneously the service, the profile and the placement key.
    """
    for path in sdl_files():
        text = path.read_text(encoding="utf-8")
        services = []
        in_services = False
        for line in text.split("\n"):
            if line.startswith("services:"):
                in_services = True
                continue
            if in_services and line and not line[0].isspace():
                break
            if in_services and len(line) - len(line.lstrip()) == 2 and line.strip().endswith(":"):
                services.append(line.strip().rstrip(":"))
        for key in placement_keys(text):
            assert key not in services, (
                f"{path.name}: placement key {key!r} collides with a service name. "
                f"Stamping it would rename the service and change the lease's service set."
            )


def test_placement_keys_parse_from_a_templated_sdl():
    """sdl/github-runner-probe.yaml does not survive yaml.safe_load (templated), which is
    why the scanner is line-based. If it ever returns nothing for that file the guard has
    gone blind for it specifically."""
    probe = [p for p in sdl_files() if p.name == "github-runner-probe.yaml"]
    assert probe, "github-runner-probe.yaml disappeared — update this test deliberately"
    assert placement_keys(probe[0].read_text(encoding="utf-8"))


def test_scanner_reads_the_key_under_placement_not_a_sibling_block():
    sdl = (
        "profiles:\n"
        "  compute:\n"
        "    web:\n"
        "      resources: {}\n"
        "  placement:\n"
        "    just-akash-web:\n"
        "      pricing:\n"
        "        web:\n"
        "          amount: 1\n"
        "deployment:\n"
        "  web:\n"
        "    just-akash-web:\n"
        "      profile: web\n"
    )
    assert placement_keys(sdl) == ["just-akash-web"]


def test_scanner_finds_multiple_placement_keys():
    sdl = (
        "profiles:\n"
        "  placement:\n"
        "    just-akash-a:\n"
        "      pricing: {}\n"
        "    just-akash-b:\n"
        "      pricing: {}\n"
    )
    assert placement_keys(sdl) == ["just-akash-a", "just-akash-b"]


def test_a_placement_inside_a_block_scalar_is_not_a_key():
    """CodeRabbit's counter-example on #150, verbatim.

    An SDL may pass a whole YAML document to a container as an argument. That is STRING
    CONTENT, not structure — treating it as a placement key would make this guard assert
    against a key that never reaches the chain.
    """
    sdl = (
        "services:\n"
        "  app:\n"
        "    args:\n"
        "      - |\n"
        "        placement:\n"
        "          just-akash-fake:\n"
        "profiles:\n"
        "  placement:\n"
        "    just-akash-real:\n"
        "      pricing: {}\n"
    )
    assert placement_keys(sdl) == ["just-akash-real"]


def test_a_placement_outside_profiles_is_not_a_key():
    """`deployment.<svc>.<KEY>` REFERENCES the placement key; it does not declare one.
    Only the declaration under `profiles:` becomes group_spec.name."""
    sdl = (
        "deployment:\n"
        "  web:\n"
        "    placement:\n"
        "      not-a-declaration:\n"
        "profiles:\n"
        "  placement:\n"
        "    just-akash-web:\n"
        "      pricing: {}\n"
    )
    assert placement_keys(sdl) == ["just-akash-web"]


def test_folded_block_scalars_are_skipped_too():
    """`>` folds, `|` literals — both are string bodies."""
    sdl = (
        "services:\n"
        "  app:\n"
        "    args:\n"
        "      - >-\n"
        "        placement:\n"
        "          just-akash-folded-fake:\n"
        "profiles:\n"
        "  placement:\n"
        "    just-akash-only:\n"
        "      pricing: {}\n"
    )
    assert placement_keys(sdl) == ["just-akash-only"]


# --------------------------------------------------------------------------
# Run scoping — the key must still match by prefix, and both copies must move
# --------------------------------------------------------------------------

RUN = "deadbeef"


def test_the_prefix_still_matches_after_scoping():
    """Everything that consumes this marker uses startswith — the sibling sweeper, this
    module's guard, cleanup_stale. A suffix that broke prefix matching would put every
    deployment back on the sweeper's reap list, which is what #150 exists to prevent."""
    scoped = run_scoped("just-akash-runner", RUN)
    assert scoped.startswith(PLACEMENT_PREFIX)
    assert not scoped.startswith(SIBLING_REAPED_PREFIX)


def test_both_copies_of_the_key_move_together():
    """The key appears twice: `profiles.placement.<KEY>` and the
    `deployment.<service>.<KEY>` reference that selects it. Rewriting one leaves an SDL
    naming a placement group that does not exist, which Akash rejects."""
    for path in sdl_files():
        text = path.read_text(encoding="utf-8")
        original = placement_keys(text)
        out, new_keys = stamp_run(text, RUN)
        assert new_keys, f"{path.name}: nothing stamped"
        assert placement_keys(out) == new_keys, path.name
        for old, new in zip(original, new_keys, strict=True):
            assert out.count(f"{new}:") >= 2, (
                f"{path.name}: {new} must appear as the declaration AND at least one "
                f"deployment reference — several services may share one placement group"
            )
            assert not re.search(rf"(?m)^[ \t]+{re.escape(old)}:[ \t]*$", out), (
                f"{path.name}: an unscoped {old} survives — the two copies disagree"
            )


def test_a_scoped_sdl_still_passes_the_prefix_guard():
    """The guard runs against files on disk, but the stamped text is what reaches Akash."""
    for path in sdl_files():
        out, _ = stamp_run(path.read_text(encoding="utf-8"), RUN)
        for key in placement_keys(out):
            assert key.startswith(PLACEMENT_PREFIX), (path.name, key)


def test_the_run_id_reads_back():
    assert run_id_of(run_scoped("just-akash-runner", RUN)) == RUN


def test_an_unscoped_or_foreign_key_reports_no_run():
    """Reading a run id out of a key that has none must return "", never a guess — a
    caller uses this to decide whether to DESTROY."""
    for key in ("just-akash-runner", "dfci-infra-runner.abc123", "dcloud", "akash", ""):
        assert run_id_of(key) == "", key


def test_a_non_hex_tail_is_not_a_run_id():
    """Workload names may contain dots. `just-akash-my.service` must not read as a
    deployment belonging to run `service`."""
    assert run_id_of("just-akash-my.service") == ""


def test_stamping_is_idempotent():
    """A re-deploy path may transform the same SDL twice; a second stamp must not append
    a second run id."""
    once, keys1 = stamp_run(sdl_files()[0].read_text(encoding="utf-8"), RUN)
    twice, keys2 = stamp_run(once, RUN)
    assert twice == once and keys2 == []
    assert all(run_id_of(k) == RUN for k in keys1)


def test_an_empty_run_id_leaves_the_sdl_untouched():
    """No run id means the previous behaviour exactly — a bare repo key."""
    text = sdl_files()[0].read_text(encoding="utf-8")
    out, keys = stamp_run(text, "")
    assert out == text and keys == []


def test_a_foreign_placement_key_is_never_rewritten():
    """`deploy` runs arbitrary caller SDLs. Stamping someone else's document would claim
    authorship we did not have, and the marker must only say what it can prove."""
    foreign = "services:\n  a:\n    image: x\nprofiles:\n  placement:\n    dcloud:\n"
    out, keys = stamp_run(foreign, RUN)
    assert out == foreign and keys == []


def test_a_commented_key_line_is_rewritten_and_keeps_its_comment():
    """The key appears twice, and a comment on only ONE of them — typically the
    `deployment.<service>.<KEY>` reference, where "# picks the placement above" is a
    natural note — would rewrite the declaration and leave the reference pointing at a
    placement group that no longer exists. Akash rejects that SDL, so a comment in a
    caller's file would break the deploy outright."""
    sdl = (
        "services:\n  a:\n    image: x\n"
        "profiles:\n  placement:\n    just-akash-a:\n"
        "deployment:\n  a:\n    just-akash-a:   # picks the placement above\n"
        "      profile: a\n      count: 1\n"
    )
    out, keys = stamp_run(sdl, RUN)
    assert keys == [f"just-akash-a.{RUN}"]
    assert f"just-akash-a.{RUN}:   # picks the placement above" in out, "comment lost"
    assert not re.search(r"(?m)^\s+just-akash-a:", out), (
        "an unscoped reference survived — the SDL now names a placement group that does not exist"
    )


def test_a_placement_group_shared_by_several_services_is_fully_rewritten():
    """Valid SDLs may point more than one service at the same placement group. Missing
    one reference leaves that service selecting a group that no longer exists."""
    sdl = (
        "services:\n  a:\n    image: x\n  b:\n    image: y\n"
        "profiles:\n  placement:\n    just-akash-a:\n"
        "deployment:\n  a:\n    just-akash-a:\n      profile: a\n      count: 1\n"
        "  b:\n    just-akash-a:\n      profile: b\n      count: 1\n"
    )
    out, _ = stamp_run(sdl, RUN)
    assert out.count(f"just-akash-a.{RUN}:") == 3
    assert not re.search(r"(?m)^\s+just-akash-a:", out)


def test_the_key_is_not_rewritten_inside_a_comment_or_block_scalar():
    """The counterpart risk: a bare substring replace would rewrite text that merely
    MENTIONS the key, which is why this anchors to a whole key line."""
    sdl = (
        "services:\n  a:\n    image: x\n    args:\n      - |\n"
        "        just-akash-a:\n"
        "profiles:\n  placement:\n    just-akash-a:\n"
        "# see just-akash-a: for the placement\n"
    )
    out, _ = stamp_run(sdl, RUN)
    assert "        just-akash-a:" in out, "block scalar content was rewritten"
    assert "# see just-akash-a: for the placement" in out, "a comment was rewritten"


def test_the_template_predicate_agrees_with_the_scanner():
    """⛔ ONE DEFINITION, ASSERTED. A caller asking "is this a template?" must get the same
    answer the scanner gave when it ACCEPTED the key. A stricter private copy — this test
    once carried `^\\$\\{[A-Z_]+\\}$` — scans `${PLACEMENT_KEY2}` as a template and then
    judges it as a literal, failing it for not starting with the repo prefix. The two must
    not be able to disagree."""
    for key in ("${PLACEMENT_KEY}", "${PLACEMENT_KEY2}", "${x}", "${_k9}"):
        sdl = f"profiles:\n  placement:\n    {key}:\n"
        assert placement_keys(sdl) == [key], f"scanner missed {key}"
        assert is_templated(key), f"predicate disagrees with the scanner on {key}"
    for key in ("just-akash-runner", "borduas", "dcloud"):
        assert not is_templated(key), f"{key} is a literal, not a template"
