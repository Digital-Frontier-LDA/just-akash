"""Dormant one-shot tenant startup; neither allocation nor publication authority.

The caller must already declare the owned suppliers' standard beta3 state mount.
This renderer never adds storage or changes resources. The original unnamed
40Gi template is held. A retained mount prevents container-start replay only;
mount loss/pod replacement remains the independent controller's UNKNOWN duty.
"""

from __future__ import annotations

import base64
import copy
import re

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .github_jit import JitHandoff, JitHold, _json
from .request_profile import _size_bytes
from .runner_image import NATIVE_READER_IMAGE
from .sentry_lease_receipt import payload_profile

# A narrower delivery bound than mint_jit's 1MiB API response bound. The encoded
# value is a single Linux environment entry, so refusing oversized delivery
# before create is preferable to an exec E2BIG after allocating the lease.
MAX_CONFIG = 64 * 1024
STATE_MOUNT = "/actions-runner/_work"
STORAGE_CLASS = "beta3"
RUNNER_ROOT = "/actions-runner"

# No Python installation or replacement image is required on the tenant.
# Bash privileged mode ignores inherited BASH_ENV/SHELLOPTS. No credential is
# interpolated into this program or its argv. Listener's supported input-env
# mechanism removes the JIT input internally before jobs start.
STARTUP = r"""set +x
set -Eeuo pipefail
umask 077
held() { printf 'JIT tenant startup held\n' >&2; exit 1; }
trap 'held' ERR
[[ $# == 0 ]] || held
[[ ${RUNNER_JIT_CONFIG+x} && -n $RUNNER_JIT_CONFIG ]] || held
[[ ${EXPECTED_RUNNER_ID:-} =~ ^[1-9][0-9]{0,15}$ ]] || held
[[ ${EXPECTED_RUNNER_NAME:-} =~ ^dfci-[a-z0-9-]{1,90}$ ]] || held
for forbidden in ACCESS_TOKEN RUNNER_TOKEN GITHUB_TOKEN GH_TOKEN SOPS_AGE_KEY \
    AKASH_API_KEY ACTIONS_RUNNER_INPUT_JITCONFIG; do
    [[ ! -v $forbidden ]] || held
done
config=$RUNNER_JIT_CONFIG
expected_id=$EXPECTED_RUNNER_ID
expected_name=$EXPECTED_RUNNER_NAME
(( ${#config} <= 65536 )) || held
(( expected_id <= 9007199254740991 )) || held
unset RUNNER_JIT_CONFIG
export -n config expected_id expected_name
canonical() {
    local value=$1 round
    [[ -n $value && $value =~ ^[A-Za-z0-9+/]+={0,2}$ ]] || held
    round=$(printf '%s' "$value" | base64 --decode 2>/dev/null | base64 | tr -d '\r\n') || held
    [[ $round == "$value" ]] || held
}
unique_object() {
    # --stream sees duplicate keys before jq's normal object decoding drops them.
    printf '%s' "$1" | jq --stream -c 'select(length == 2) | .[0]' 2>/dev/null \
        | jq -se 'length > 0 and (length == (unique | length))' >/dev/null 2>&1 || held
}
canonical "$config"
decoded=$(printf '%s' "$config" | base64 --decode 2>/dev/null; printf '.')
decoded=${decoded%.}
unique_object "$decoded"
printf '%s' "$decoded" | jq -se '
    length == 1 and (.[0] | type == "object" and length > 0 and length <= 64
    and has(".runner") and all(to_entries[];
        (.key | test("^[A-Za-z0-9_.-]{1,128}$")) and .key != "." and .key != ".."
        and (.value | type == "string" and length > 0)))' >/dev/null 2>&1 || held
while IFS= read -r value; do canonical "$value"; done \
    < <(printf '%s' "$decoded" | jq -r '.[]' 2>/dev/null)
settings_encoded=$(printf '%s' "$decoded" | jq -r '.[".runner"]' 2>/dev/null)
settings=$(printf '%s' "$settings_encoded" | base64 --decode 2>/dev/null; printf '.')
settings=${settings%.}
unique_object "$settings"
# jq accepts non-JSON numbers such as NaN and erases float lexemes. Settings
# are deliberately scalar-only; validate their raw JSON token grammar first.
printf '%s' "$settings" | jq -Rse '
    gsub("\"([^\"\\\\]|\\\\.)*\""; "\"\"")
    | gsub("[[:space:]]"; "")
    | "(\"\":(\"\"|true|false|null|0|[1-9][0-9]*))" as $pair
    | test("^\\{" + $pair + "(," + $pair + ")*\\}$")' >/dev/null 2>&1 || held
printf '%s' "$settings" | jq -se --arg id "$expected_id" --arg name "$expected_name" '
    length == 1 and (.[0] | type == "object"
    and all(.[]; type == "string" or type == "boolean" or type == "null"
        or (type == "number" and . == floor and . >= 0 and . <= 9007199254740991))
    and (.agentId | type == "number" and . > 0 and . == floor and tostring == $id)
    and .agentName == $name and .ephemeral == true
    and .workFolder == "_work")' >/dev/null 2>&1 || held
[[ -d /actions-runner/_work && ! -L /actions-runner/_work && -O /actions-runner/_work ]] || held
# Require a distinct real filesystem mount, not an ordinary root directory or
# RAM/overlay state. Class/provider/PVC provenance still needs hosted evidence.
awk '$5 == "/actions-runner/_work" { n++; for (i=6;i<=NF;i++) if ($i == "-") {
    if ($(i+1) == "tmpfs" || $(i+1) == "ramfs" || $(i+1) == "overlay") bad=1
}} END { exit(n != 1 || bad) }' /proc/self/mountinfo >/dev/null 2>&1 || held
mode=$(stat -c '%a' /actions-runner/_work 2>/dev/null) || held
[[ $mode =~ ^[0-7]{3,4}$ ]] || held
(( (8#$mode & 8#022) == 0 )) || held
[[ -d /actions-runner && ! -L /actions-runner && -x /actions-runner/bin/Runner.Listener ]] || held
cd /actions-runner
while IFS= read -r filename; do
    [[ ! -e $filename && ! -L $filename ]] || held
done < <(printf '%s' "$decoded" | jq -r 'keys[]' 2>/dev/null)
# mkdir is exclusive. An interrupted/failed start remains consumed, including
# ambiguous exec or Listener failure. Never clear this marker or remint locally.
mkdir -m 700 /actions-runner/_work/.jit-consumed 2>/dev/null || held
printf 'runner_id=%s\nrunner_name=%s\n' "$expected_id" "$expected_name" \
    > /actions-runner/_work/.jit-consumed/identity
sync -f /actions-runner/_work >/dev/null 2>&1 || held
unset decoded settings settings_encoded value filename
while IFS= read -r variable; do export -n "${variable?}"; done < <(compgen -e)
export PATH=/usr/bin:/bin HOME=/root LANG=C RUNNER_ALLOW_RUNASROOT=1
export AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache
export ACTIONS_RUNNER_INPUT_JITCONFIG=$config
unset config
exec /actions-runner/bin/Runner.Listener run >/dev/null 2>&1
"""


def _require(condition: object) -> None:
    if not condition:
        raise JitHold("Sentry JIT tenant payload is unverified")


def validate_handoff(handoff: JitHandoff) -> None:
    """Check actual nested settings; construction is never mint/admission proof."""
    try:
        _require(isinstance(handoff, JitHandoff))
        _require(type(handoff.runner_id) is int and 0 < handoff.runner_id <= 2**53 - 1)
        _require(re.fullmatch(r"dfci-[a-z0-9-]{1,90}", handoff.runner_name))
        _require(type(handoff.group_id) is int and handoff.group_id > 0)
        _require(re.fullmatch(r"[0-9a-f]{40}", handoff.policy_revision))
        _require(type(handoff.labels) is tuple and handoff.runner_name in handoff.labels)
        _require(len(handoff.labels) == len(set(handoff.labels)) <= 100)
        _require(all(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", label) for label in handoff.labels))
        _require(isinstance(handoff.encoded_config, str))
        _require(0 < len(handoff.encoded_config) <= MAX_CONFIG)
        raw = base64.b64decode(handoff.encoded_config, validate=True)
        _require(base64.b64encode(raw).decode() == handoff.encoded_config)
        document = _json(raw)
        _require(isinstance(document, dict) and 0 < len(document) <= 64)
        _require(".runner" in document)
        files = {}
        for name, encoded in document.items():
            _require(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) and name not in {".", ".."})
            _require(isinstance(encoded, str) and encoded)
            data = base64.b64decode(encoded, validate=True)
            _require(data and base64.b64encode(data).decode() == encoded)
            files[name] = data
        settings = _json(files[".runner"])
        _require(isinstance(settings, dict))
        _require(
            all(
                type(value) in (str, bool, type(None))
                or type(value) is int
                and 0 <= value <= 2**53 - 1
                for value in settings.values()
            )
        )
        _require(type(settings.get("agentId")) is int and settings["agentId"] == handoff.runner_id)
        _require(settings.get("agentName") == handoff.runner_name)
        _require(settings.get("ephemeral") is True)
        _require(settings.get("workFolder") == "_work")
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise JitHold("Sentry JIT tenant payload is unverified") from None


def _document(sdl: str) -> dict:
    try:
        _require(isinstance(sdl, str) and 0 < len(sdl.encode()) <= 256 * 1024)
        _require(not any(isinstance(event, AliasEvent) for event in yaml.parse(sdl)))

        def walk(node):
            if isinstance(node, MappingNode):
                keys = []
                for key, value in node.value:
                    _require(isinstance(key, ScalarNode) and key.tag == "tag:yaml.org,2002:str")
                    _require(key.value not in keys and key.value != "<<")
                    keys.append(key.value)
                    walk(value)
            elif isinstance(node, SequenceNode):
                for item in node.value:
                    walk(item)

        walk(yaml.compose(sdl))
        document = yaml.safe_load(sdl)
        _require(isinstance(document, dict))
        return document
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise JitHold("Sentry JIT tenant SDL is unverified") from None


def _render(sdl: str, handoff: JitHandoff, *, enable_existing_state_mount: bool) -> str:
    """Secret SDL, not log/artifact data. No storage mutation or effect permission.

    Explicit beta3 persistent storage must already exist in the caller's SDL.
    This validates a requested shape only, not supplier capacity/PVC provenance.
    All original profiles/deployment bytes are retained; only services change.
    """
    _require(enable_existing_state_mount is True)
    validate_handoff(handoff)
    document = _document(sdl)
    _require(list(document) == ["version", "services", "profiles", "deployment"])
    _require(document["version"] == "2.0" and set(document["services"]) == {"runner"})
    service = document["services"]["runner"]
    _require(
        isinstance(service, dict) and set(service) <= {"image", "credentials", "env", "params"}
    )
    _require(service.get("image") == NATIVE_READER_IMAGE)
    _require(service.get("params") == {"storage": {"jit-state": {"mount": STATE_MOUNT}}})
    resources = document["profiles"]["compute"]["runner"]["resources"]
    before = copy.deepcopy(resources)
    volumes = resources.get("storage")
    _require(isinstance(volumes, list) and len(volumes) == 1)
    state = volumes[0]
    _require(isinstance(state, dict) and set(state) == {"name", "size", "attributes"})
    _require(state["name"] == "jit-state")
    _require(state["attributes"] == {"persistent": True, "class": STORAGE_CLASS})
    _require(_size_bytes(state["size"], "work/state") == 40 * 1024**3)
    group, _ = payload_profile(sdl)
    _require(document["deployment"] == {"runner": {group: {"profile": "runner", "count": 1}}})
    service["command"] = ["/bin/bash", "--noprofile", "--norc", "-p", "-c", STARTUP]
    service["env"] = [
        "RUNNER_JIT_CONFIG=" + handoff.encoded_config,
        "EXPECTED_RUNNER_ID=" + str(handoff.runner_id),
        "EXPECTED_RUNNER_NAME=" + handoff.runner_name,
    ]
    _require(resources == before)
    headers = list(re.finditer(r"^profiles:[ \t]*$", sdl, re.MULTILINE))
    _require(len(headers) == 1)
    prefix = yaml.safe_dump(
        {"version": document["version"], "services": document["services"]}, sort_keys=False
    )
    result = prefix + sdl[headers[0].start() :]
    _require(_document(result)["profiles"] == document["profiles"])
    _require(payload_profile(result)[0] == group)
    return result


def render(sdl: str, handoff: JitHandoff, *, enable_existing_state_mount: bool = False) -> str:
    """Opt-in source ingredient only; return secret SDL, never an admission verdict."""
    try:
        return _render(sdl, handoff, enable_existing_state_mount=enable_existing_state_mount)
    except (KeyError, TypeError, ValueError, AttributeError, RuntimeError, RecursionError):
        raise JitHold("Sentry JIT tenant SDL is unverified") from None
