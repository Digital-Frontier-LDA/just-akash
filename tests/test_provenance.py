"""Every SDL must stamp this repo's provenance prefix, or the sweeper deletes it.

WHAT THIS GUARDS. Our Console wallet is shared with Blazing-Back, whose leak sweeper runs
every 3 hours and closes any non-GPU deployment older than 12 hours. Five canary leases
were destroyed that way on 2026-08-11/12 — measured on chain, every closure a
MsgCloseDeployment from our own wallet — and df-grafana paged three innocent providers for
it. An SDL added later without the prefix is not a style problem; it is a deployment that
gets deleted every ~12 hours and a metric that lies about whose fault it was.
"""

from __future__ import annotations

from just_akash.provenance import (
    PLACEMENT_PREFIX,
    SIBLING_REAPED_PREFIX,
    placement_keys,
    sdl_files,
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
