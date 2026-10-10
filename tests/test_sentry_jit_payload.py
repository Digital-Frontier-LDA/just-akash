"""Execute the distinct one-shot startup with an inert Listener, never a tenant."""

import base64
import copy
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
import yaml

from just_akash import sentry_jit_payload as payload
from just_akash.github_jit import JitHandoff, JitHold
from just_akash.runner_image import NATIVE_READER_IMAGE
from just_akash.sentry_lease_receipt import payload_profile

NAME = "dfci-sentry-123-2-slot1"
GROUP = "borduas-sentry-idv1-class-ci-runner-g1-attempt-2-run-123-end"
SETTINGS = {"agentId": 12345, "agentName": NAME, "ephemeral": True, "workFolder": "_work"}
BASH = shutil.which("bash")


def encoded(value):
    return base64.b64encode(
        value if isinstance(value, bytes) else json.dumps(value).encode()
    ).decode()


def handoff(settings=None):
    return JitHandoff(
        12345,
        NAME,
        7,
        "a" * 40,
        ("self-hosted", "linux", "akash", NAME),
        encoded(
            {
                ".runner": encoded(SETTINGS if settings is None else settings),
                ".credentials": encoded({"credential": "synthetic-single-slot-secret"}),
            }
        ),
    )


def candidate():
    # Distinct single40Gi beta3 work/state candidate, NOT the legacy ephemeral
    # root route or proof that any owned supplier mounted/preserved this volume.
    return {
        "version": "2.0",
        "services": {
            "runner": {
                "image": NATIVE_READER_IMAGE,
                "params": {"storage": {"jit-state": {"mount": "/actions-runner/_work"}}},
            }
        },
        "profiles": {
            "compute": {
                "runner": {
                    "resources": {
                        "cpu": {"units": 2},
                        "memory": {"size": "6Gi"},
                        "storage": [
                            {
                                "name": "jit-state",
                                "size": "40Gi",
                                "attributes": {"persistent": True, "class": "beta3"},
                            },
                        ],
                    }
                }
            },
            "placement": {GROUP: {"pricing": {"runner": {"denom": "uact", "amount": 100000}}}},
        },
        "deployment": {"runner": {GROUP: {"profile": "runner", "count": 1}}},
    }


def dump(document):
    return yaml.safe_dump(document, sort_keys=False)


def test_renderer_is_off_by_default_and_original_unnamed_40gi_route_remains_held():
    with pytest.raises(JitHold):
        payload.render(dump(candidate()), handoff())
    original = candidate()
    original["profiles"]["compute"]["runner"]["resources"]["storage"] = {"size": "40Gi"}
    original["services"]["runner"].pop("params")
    with pytest.raises(JitHold):
        payload.render(dump(original), handoff(), enable_existing_state_mount=True)


def test_renderer_preserves_exact_resource_tail_bytes_and_uses_no_credential_argv():
    source = dump(candidate())
    rendered = payload.render(source, handoff(), enable_existing_state_mount=True)
    assert rendered[rendered.index("profiles:\n") :] == source[source.index("profiles:\n") :]
    document = yaml.safe_load(rendered)
    assert document["profiles"] == candidate()["profiles"]
    assert document["deployment"] == candidate()["deployment"]
    service = document["services"]["runner"]
    assert service["image"] == NATIVE_READER_IMAGE
    assert service["command"] == [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-p",
        "-c",
        payload.STARTUP,
    ]
    assert handoff().encoded_config not in " ".join(service["command"])
    assert service["env"] == [
        "RUNNER_JIT_CONFIG=" + handoff().encoded_config,
        "EXPECTED_RUNNER_ID=12345",
        "EXPECTED_RUNNER_NAME=" + NAME,
    ]
    assert payload_profile(rendered)[0] == GROUP


@pytest.mark.parametrize(
    "bad",
    [
        "cpu",
        "memory",
        "storage",
        "replicas",
        "image",
        "mount",
        "ram",
        "nonpersistent",
        "default-class",
        "zero-state",
        "extra-service",
        "duplicate-yaml",
        "alias-yaml",
        "extra-command",
    ],
)
def test_renderer_refuses_unverified_shape_or_storage_without_mutating_input(bad):
    document = candidate()
    resources = document["profiles"]["compute"]["runner"]["resources"]
    if bad == "cpu":
        resources["cpu"]["units"] = 1
    elif bad == "memory":
        resources["memory"]["size"] = "3Gi"
    elif bad == "storage":
        resources["storage"][0]["size"] = "41Gi"
    elif bad == "replicas":
        document["deployment"]["runner"][GROUP]["count"] = 2
    elif bad == "image":
        document["services"]["runner"]["image"] = "public.invalid/other:latest"
    elif bad == "mount":
        document["services"]["runner"]["params"]["storage"]["jit-state"]["mount"] = "/other"
    elif bad == "ram":
        resources["storage"][0]["attributes"]["class"] = "ram"
    elif bad == "nonpersistent":
        resources["storage"][0]["attributes"]["persistent"] = False
    elif bad == "default-class":
        resources["storage"][0]["attributes"]["class"] = "default"
    elif bad == "zero-state":
        resources["storage"][0]["size"] = "0Gi"
    elif bad == "extra-service":
        document["services"]["second"] = copy.deepcopy(document["services"]["runner"])
    elif bad == "extra-command":
        document["services"]["runner"]["command"] = ["unsafe"]
    source = dump(document)
    if bad == "duplicate-yaml":
        source = source.replace("units: 2", "units: 2\n          units: 2")
    if bad == "alias-yaml":
        source = source.replace("cpu:", "cpu: &bound").replace(
            "memory:\n", "memory: *bound\n          ignored:\n"
        )
    before = source
    with pytest.raises(JitHold):
        payload.render(source, handoff(), enable_existing_state_mount=True)
    assert source == before


@pytest.fixture
def startup(tmp_path):
    root, state, tools = (tmp_path / name for name in ("runner", "state", "tools"))
    for path in (root, state, tools, root / "bin"):
        path.mkdir(mode=0o700)
    mount = tmp_path / "mountinfo"
    mount.write_text(f"10 9 8:1 / {state} rw - ext4 /dev/fake rw\n")
    trace, sync_trace = tmp_path / "listener.jsonl", tmp_path / "sync.jsonl"
    script = tmp_path / "startup.sh"
    script.write_text(
        payload.STARTUP.replace("/actions-runner/_work", str(state))
        .replace("/actions-runner", str(root))
        .replace("/proc/self/mountinfo", str(mount))
    )
    listener = root / "bin/Runner.Listener"
    configured = []

    def executable(path, text):
        path.write_text(text)
        path.chmod(0o700)

    executable(tools / "stat", "#!/bin/sh\nprintf '700\\n'\n")
    executable(tools / "sync", f"#!/bin/sh\nprintf 'flushed\\n' >> '{sync_trace}'\n")

    def run(*, config=None, extra=None, exitcode=0):
        if not configured or configured[0] != exitcode:
            executable(
                listener,
                f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
                f"trace=Path({str(trace)!r})\nrecord={{'argv':sys.argv[1:],'env':sorted(os.environ),\n"
                f"'jit_received':os.environ.get('ACTIONS_RUNNER_INPUT_JITCONFIG')=={handoff().encoded_config!r},\n"
                f"'marker_exists':Path({str(state / '.jit-consumed/identity')!r}).is_file(),\n"
                f"'sync_exists':Path({str(sync_trace)!r}).is_file()}}\n"
                "with trace.open('a') as f: f.write(json.dumps(record)+'\\n')\n"
                "print(os.environ.get('ACTIONS_RUNNER_INPUT_JITCONFIG',''),file=sys.stderr)\n"
                "print(os.environ.get('ACTIONS_RUNNER_INPUT_JITCONFIG',''))\n"
                f"sys.exit({exitcode})\n",
            )
            configured[:] = [exitcode]
        env = {
            "PATH": str(tools) + ":" + os.environ["PATH"],
            "RUNNER_JIT_CONFIG": handoff().encoded_config if config is None else config,
            "EXPECTED_RUNNER_ID": "12345",
            "EXPECTED_RUNNER_NAME": NAME,
            "UNRELATED_MANAGEMENT_SECRET": "synthetic-management-do-not-forward",
        }
        env.update(extra or {})
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-p", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert handoff().encoded_config not in result.stdout + result.stderr
        assert "synthetic-management-do-not-forward" not in result.stdout + result.stderr
        rows = (
            [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        )
        return result, rows

    return run, root, state, mount, tools, sync_trace


@pytest.mark.parametrize("exitcode", [0, 1, 2, 3, 4, 7])
def test_actual_fake_listener_exec_is_once_with_private_env_and_durable_marker_before_exec(
    startup, exitcode
):
    run, _, state, _, _, _ = startup
    result, rows = run(exitcode=exitcode)
    assert result.returncode == exitcode
    assert len(rows) == 1 and rows[0]["argv"] == ["run"]
    assert rows[0]["jit_received"] and rows[0]["marker_exists"] and rows[0]["sync_exists"]
    assert set(rows[0]["env"]) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_CTYPE",
        "RUNNER_ALLOW_RUNASROOT",
        "AGENT_TOOLSDIRECTORY",
        "ACTIONS_RUNNER_INPUT_JITCONFIG",
        "SHLVL",  # Bash regenerates this non-secret bookkeeping entry on exec.
        # Darwin/Python inserts this process-local value; it was not forwarded
        # from the supplied environment. Linux Listener does not require it.
        *({"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()),
    }
    assert (
        state / ".jit-consumed/identity"
    ).read_text() == "runner_id=12345\nrunner_name=" + NAME + "\n"
    assert result.stdout == result.stderr == ""
    second, rows = run(exitcode=exitcode)
    assert second.returncode != 0 and len(rows) == 1


def test_concurrent_starts_execute_at_most_one_fake_listener(startup):
    run, _, _, _, _, _ = startup
    refused, rows = run(config="")  # Prepare the inert executable, no marker.
    assert refused.returncode != 0 and rows == []
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: run(), range(6)))
    assert sum(result.returncode == 0 for result, _ in results) == 1
    assert len(run()[1]) == 1


@pytest.mark.parametrize(
    "key",
    [
        "ACCESS_TOKEN",
        "RUNNER_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "SOPS_AGE_KEY",
        "AKASH_API_KEY",
        "ACTIONS_RUNNER_INPUT_JITCONFIG",
    ],
)
@pytest.mark.parametrize("value", ["", "synthetic-management-secret"])
def test_token_or_management_environment_has_no_legacy_fallback(startup, key, value):
    run, _, state, _, _, _ = startup
    result, rows = run(extra={key: value})
    assert result.returncode != 0 and rows == [] and not (state / ".jit-consumed").exists()
    assert value == "" or value not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "settings",
    [
        dict(SETTINGS, agentId=999),
        dict(SETTINGS, agentId="12345"),
        dict(SETTINGS, agentId=12345.0),
        dict(SETTINGS, extra=float("nan")),
        dict(SETTINGS, extra=float("inf")),
        dict(SETTINGS, agentName="dfci-other"),
        dict(SETTINGS, ephemeral=False),
        dict(SETTINGS, ephemeral="true"),
        {"AgentId": 12345, "AgentName": NAME, "Ephemeral": True},
    ],
)
def test_actual_nested_identity_and_ephemeral_must_match_host_and_tenant(startup, settings):
    bad = handoff(settings)
    with pytest.raises(JitHold):
        payload.validate_handoff(bad)
    run, _, state, _, _, _ = startup
    result, rows = run(config=bad.encoded_config)
    assert result.returncode != 0 and rows == [] and not (state / ".jit-consumed").exists()
    assert bad.encoded_config not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "!invalid!",
        encoded({}),
        encoded({"../outside": encoded("secret")}),
        encoded({".runner": "AB=="}),
        encoded(b'{".runner":"YQ==",".runner":"Yg=="}'),
        encoded(
            {
                ".runner": encoded(
                    b'{"agentId":12345,"agentId":12345,"agentName":"'
                    + NAME.encode()
                    + b'","ephemeral":true}'
                )
            }
        ),
        "A" * (payload.MAX_CONFIG + 1),
    ],
    ids=[
        "empty",
        "invalid",
        "empty-map",
        "traversal",
        "noncanonical-inner",
        "duplicate-map",
        "duplicate-settings",
        "delivery-bound",
    ],
)
def test_canonical_bounded_unique_configuration_is_required_before_any_exec(startup, bad):
    with pytest.raises(JitHold):
        payload.validate_handoff(replace(handoff(), encoded_config=bad))
    run, _, state, _, _, _ = startup
    result, rows = run(config=bad)
    assert result.returncode != 0 and rows == [] and not (state / ".jit-consumed").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "absent-mount",
        "ram-mount",
        "overlay-mount",
        "duplicate-mount",
        "existing-config",
        "state-symlink",
        "sync-failure",
    ],
)
def test_missing_or_unsafe_state_and_ambiguous_durability_never_execute_listener(startup, bad):
    run, root, state, mount, tools, _ = startup
    if bad == "absent-mount":
        mount.write_text("")
    elif bad == "ram-mount":
        mount.write_text(mount.read_text().replace("ext4", "tmpfs"))
    elif bad == "overlay-mount":
        mount.write_text(mount.read_text().replace("ext4", "overlay"))
    elif bad == "duplicate-mount":
        mount.write_text(mount.read_text() * 2)
    elif bad == "existing-config":
        (root / ".runner").write_text("previous-config")
    elif bad == "state-symlink":
        actual = state.with_name("actual-state")
        state.rename(actual)
        state.symlink_to(actual, target_is_directory=True)
    elif bad == "sync-failure":
        (tools / "sync").write_text("#!/bin/sh\nexit 1\n")
    result, rows = run()
    assert result.returncode != 0 and rows == []
    assert (state / ".jit-consumed").exists() is (bad == "sync-failure")
    if bad == "sync-failure":
        result, rows = run()
        assert result.returncode != 0 and rows == []


def test_split_storage_does_not_trade_original_40gi_request_for_smaller_root():
    source = candidate()
    source["profiles"]["compute"]["runner"]["resources"]["storage"] = [
        {"size": "39Gi"},
        {"name": "jit-state", "size": "1Gi", "attributes": {"persistent": True, "class": "beta3"}},
    ]
    with pytest.raises(JitHold):
        payload.render(dump(source), handoff(), enable_existing_state_mount=True)


@pytest.mark.parametrize("folder", ["/_work", "../_work", "work", "", None])
def test_existing_mint_relative_work_folder_is_bound_to_exact_mounted_work_tree(startup, folder):
    settings = {**SETTINGS, "workFolder": folder}
    value = handoff(settings)
    with pytest.raises(JitHold):
        payload.validate_handoff(value)
    run, _, state, _, _, _ = startup
    result, rows = run(config=value.encoded_config)
    assert result.returncode != 0 and rows == [] and not (state / ".jit-consumed").exists()


@pytest.mark.parametrize("text", ['escaped " quote', "back\\slash", "line\nnext", "café"])
def test_strict_primitive_json_settings_accept_real_escaped_strings(startup, text):
    value = handoff({**SETTINGS, "serverUrl": text})
    payload.validate_handoff(value)
    run, _, _, _, _, _ = startup
    result, rows = run(config=value.encoded_config)
    assert result.returncode == 0 and len(rows) == 1


def test_exec_failure_after_durable_claim_is_silent_and_never_retried(startup):
    run, root, state, _, tools, sync_trace = startup
    result, rows = run(config="")  # Prepare the inert Listener without consuming.
    assert result.returncode != 0 and rows == []
    (tools / "sync").write_text(
        f"#!/bin/sh\nrm '{root / 'bin/Runner.Listener'}'\nprintf 'flushed\\n' >> '{sync_trace}'\n"
    )
    result, rows = run()
    assert result.returncode != 0 and rows == []
    assert (state / ".jit-consumed/identity").is_file()
    assert result.stdout == result.stderr == ""
    again, rows = run()
    assert again.returncode != 0 and rows == []


def test_inherited_shell_startup_and_trace_controls_cannot_execute(startup):
    run, root, _, _, _, _ = startup
    attack, witness = root / "attacker.sh", root / "attacked"
    attack.write_text(f"touch '{witness}'\nset -x\n")
    result, rows = run(extra={"BASH_ENV": str(attack), "SHELLOPTS": "xtrace", "ENV": str(attack)})
    assert result.returncode == 0 and len(rows) == 1 and not witness.exists()
    assert not {"BASH_ENV", "SHELLOPTS", "ENV"}.intersection(rows[0]["env"])
