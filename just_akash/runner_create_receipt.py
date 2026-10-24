"""Transport the existing create journal for one closed Sentry production role.

An uploaded source-bound candidate is recovery evidence, never close authority.
No create/lease/registry operation or independent artifact client lives here.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

CONTRACT = "sentry-private-production/v1"
REPOSITORY = "Borduas-Holdings/Blazing-Back"
REPOSITORY_ID = 1071436278
SDK = "Digital-Frontier-LDA/just-akash"
CALLER = ".github/workflows/build-sentry-cli.yml"
SDK_WORKFLOW = ".github/workflows/runner-pool.yml"
NATIVE_JOB = (
    "owned-production / Provision the fixed owned Sentry runner / "
    "Provision ephemeral runner pool on Akash"
)
SOURCE_FILES = (
    ".github/workflows/runner-pool.yml",
    "just_akash/runner_create_receipt.py",
    "just_akash/__init__.py",
    "just_akash/_confidential.py",
    "just_akash/_diagnostics.py",
    "just_akash/_e2e.py",
    "just_akash/_lease_verification.py",
    "just_akash/_states.py",
    "just_akash/address.py",
    "just_akash/api.py",
    "just_akash/chain.py",
    "just_akash/charter.py",
    "just_akash/cleanup_identity.py",
    "just_akash/cli.py",
    "just_akash/deploy.py",
    "just_akash/deployment_receipt.py",
    "just_akash/finalized_closure.py",
    "just_akash/owner_lookup.py",
    "just_akash/paid_create.py",
    "just_akash/provenance.py",
    "just_akash/provider_capacity.py",
    "just_akash/request_profile.py",
    "just_akash/runner_image.py",
    "just_akash/runner_repository.py",
    "just_akash/sdl_validate.py",
    "just_akash/smoke_providers.py",
    "just_akash/wallet_pool.py",
    "just_akash/workload_identity.py",
    "pyproject.toml",
)

LIMIT = 2 * 1024**2
RECEIPT_LIMIT = 64 * 1024
MAX_ROUNDS = 3


def require(value):
    if not value:
        raise ValueError("OWNED_CREATE_RECEIPT_HOLD_NO_RETRY")


def document(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False))


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def held(path, maximum, *, private=False, allow_empty=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1
        )
        require(
            (before.st_size >= 0 if allow_empty else before.st_size > 0)
            and before.st_size <= maximum
            and not before.st_mode & 0o022
        )
        require(not private or stat.S_IMODE(before.st_mode) == 0o600)
        raw = os.pread(fd, maximum + 1, 0)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        require(len(raw) == before.st_size)
        require(
            all(
                getattr(before, key) == getattr(os.fstat(fd), key) == getattr(path.lstat(), key)
                for key in fields
            )
        )
        return raw
    finally:
        os.close(fd)


def directory(path):
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )
    return path


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create(path, value):
    raw = canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def identity(env):
    require(env.get("RUNNER_CREATE_RECEIPT_CONTRACT") == CONTRACT)
    require(
        env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
    )
    require(
        env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") in {"push", "workflow_dispatch"}
    )
    require(
        env.get("GITHUB_JOB") == "pool"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and env.get("RUNNER_OS") == "Linux"
    )
    require(env.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + CALLER + "@refs/heads/main")
    sha, sdk_sha = env.get("GITHUB_SHA", ""), env.get("RUNNER_RECEIPT_SDK_REF", "")
    require(
        re.fullmatch("[0-9a-f]{40}", sha) is not None and env.get("GITHUB_WORKFLOW_SHA") == sha
    )
    require(re.fullmatch("[0-9a-f]{40}", sdk_sha) is not None)
    numbers = [env.get(key, "") for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")]
    require(all(re.fullmatch("[1-9][0-9]{0,18}", x) and int(x) < 2**63 for x in numbers))
    run, attempt = map(int, numbers)
    label = f"sentry-production-{run}-{attempt}"
    group = f"borduas-owned-sentry-a{attempt}-run-{run}-end"
    require(env.get("RUNNER_RECEIPT_LABEL") == label and env.get("RUNNER_RECEIPT_GROUP") == group)
    require(env.get("RUNNER_RECEIPT_TAG") == "ci-blazing-back-sentry-production")
    return {
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "run_id": run,
        "run_attempt": attempt,
        "source_sha": sha,
        "sdk_sha": sdk_sha,
        "caller": CALLER,
        "runner_label": label,
        "group": group,
        "namespace_authority": "not-applicable-direct-Akash",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("OWNED_CREATE_RECEIPT_HOLD_NO_RETRY")


def get(path, token):
    require(
        type(path) is str
        and re.fullmatch(
            r"/repos/Borduas-Holdings/Blazing-Back(?:/branches/main|/actions/runs/[1-9][0-9]{0,18}(?:/attempts/[1-9][0-9]{0,18}/jobs\?per_page=100&page=[1-9][0-9]?)?)?",
            path,
        )
        is not None
    )
    require(type(token) is str and bool(token) and not any(x in token for x in "\r\n\0"))
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            require(response.status == 200)
            raw = response.read(LIMIT + 1)
        require(len(raw) <= LIMIT)
        return document(raw)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError("OWNED_CREATE_RECEIPT_HOLD_NO_RETRY") from None
    except (OSError, http.client.HTTPException):
        raise ValueError("OWNED_CREATE_RECEIPT_HOLD_NO_RETRY") from None


def native(context, env, getter=get):
    base = "/repos/" + REPOSITORY
    token = env["RUNNER_RECEIPT_READ_TOKEN"]
    repo = getter(base, token)
    require(
        type(repo) is dict
        and type(repo.get("id")) is int
        and repo["id"] == REPOSITORY_ID
        and repo.get("full_name") == REPOSITORY
        and repo.get("private") is True
        and repo.get("visibility") == "private"
    )
    run = getter(base + "/actions/runs/" + str(context["run_id"]), token)
    require(
        type(run) is dict
        and type(run.get("id")) is int
        and run["id"] == context["run_id"]
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == context["run_attempt"]
    )
    require(
        run.get("head_sha") == context["source_sha"]
        and run.get("head_branch") == "main"
        and run.get("path") == CALLER
        and run.get("event") == env["GITHUB_EVENT_NAME"]
        and run.get("status") == "in_progress"
    )
    for key in ("repository", "head_repository"):
        row = run.get(key)
        require(
            type(row) is dict
            and type(row.get("id")) is int
            and row["id"] == REPOSITORY_ID
            and row.get("full_name") == REPOSITORY
        )
    references = cast(list[dict[str, Any]], run.get("referenced_workflows"))
    require(
        type(references) is list
        and len(references) <= 100
        and all(type(r) is dict for r in references)
    )
    expected = SDK + "/" + SDK_WORKFLOW + "@" + context["sdk_sha"]
    matches = [
        r
        for r in references
        if type(r.get("path")) is str and r["path"].startswith(SDK + "/" + SDK_WORKFLOW + "@")
    ]
    require(
        len(matches) == 1
        and matches[0].get("path") == expected
        and matches[0].get("sha") == context["sdk_sha"]
    )
    branch = getter(base + "/branches/main", token)
    require(
        type(branch) is dict
        and branch.get("name") == "main"
        and branch.get("protected") is True
        and branch.get("commit", {}).get("sha") == context["source_sha"]
    )
    rows: list[dict[str, Any]] = []
    total = None
    for page in range(1, 100):
        value = cast(
            dict[str, Any],
            getter(
                f"{base}/actions/runs/{context['run_id']}/attempts/{context['run_attempt']}/jobs?per_page=100&page={page}",
                token,
            ),
        )
        require(
            type(value) is dict
            and type(value.get("total_count")) is int
            and 0 < value["total_count"] <= 9900
            and type(value.get("jobs")) is list
            and len(value["jobs"]) <= 100
        )
        if total is None:
            total = value["total_count"]
        require(value["total_count"] == total)
        rows.extend(value["jobs"])
        if len(rows) >= total:
            break
        require(bool(value["jobs"]))
    require(
        len(rows) == total
        and all(type(r) is dict and type(r.get("id")) is int and r["id"] > 0 for r in rows)
    )
    require(len({r["id"] for r in rows}) == total)
    matches = [r for r in rows if r.get("name") == NATIVE_JOB]
    require(len(matches) == 1)
    job = matches[0]
    require(
        type(job.get("run_id")) is int
        and job["run_id"] == context["run_id"]
        and type(job.get("run_attempt")) is int
        and job["run_attempt"] == context["run_attempt"]
        and job.get("head_sha") == context["source_sha"]
        and job.get("status") == "in_progress"
    )
    require(
        type(job.get("labels")) is list
        and len(job["labels"]) <= 32
        and all(type(v) is str for v in job["labels"])
        and "ubuntu-latest" in job["labels"]
    )
    require(
        type(env.get("RUNNER_NAME")) is str
        and 0 < len(env["RUNNER_NAME"]) <= 256
        and job.get("runner_name") == env["RUNNER_NAME"]
    )
    return job["id"]


def sdk_capture(context, root):
    """Only explicitly named public code; no full checkout/export or secret paths."""
    command = ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"]
    result = subprocess.run(  # noqa: S603 - fixed Git argv and closed public source paths
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False
    )  # noqa: S603 - fixed Git argv, closed public paths, no shell
    require(result.returncode == 0 and result.stdout == (context["sdk_sha"] + "\n").encode())
    result = {}
    for name in SOURCE_FILES:
        raw = held(root / name, LIMIT, allow_empty=name == "just_akash/__init__.py")
        command = ["/usr/bin/git", "-C", str(root), "show", context["sdk_sha"] + ":" + name]
        p = subprocess.run(  # noqa: S603 - fixed Git argv and closed public source paths
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False
        )  # noqa: S603 - fixed Git argv, closed public paths, no shell
        require(p.returncode == 0 and len(p.stdout) <= LIMIT and p.stdout == raw)
        result[name] = raw
    return result


def storage(env, context):
    root = Path(env["RUNNER_TEMP"])
    info = root.lstat()
    require(
        root.is_absolute()
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o022
    )
    return root / f"owned-sentry-create-{context['run_id']}-{context['run_attempt']}"


def begin(env, root, getter=get):
    context = identity(env)
    context["native_job_id"] = native(context, env, getter)
    sources = sdk_capture(context, root)
    context["sdk_source_pins"] = {n: hashlib.sha256(raw).hexdigest() for n, raw in sources.items()}
    folder = storage(env, context)
    folder.mkdir(mode=0o700)
    fsync_directory(folder.parent)
    create(folder / "source.json", context)
    return context


def journal_decoder(context, root):
    sources = sdk_capture(context, root)
    require(
        {n: hashlib.sha256(raw).hexdigest() for n, raw in sources.items()}
        == context["sdk_source_pins"]
    )
    modules = {}
    # Execute exact captured original address and journal bytes, never a re-opened path.
    for name in ("address", "deployment_receipt"):
        module_name = "just_akash." + name
        module = types.ModuleType(module_name)
        module.__file__ = str(root / "just_akash" / (name + ".py"))
        module.__package__ = "just_akash"
        prior = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            # Captured fixed public paths must match the admitted immutable SDK commit.
            # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
            exec(  # noqa: S102 - exact captured native SDK commit bytes, closed modules
                compile(sources["just_akash/" + name + ".py"], module.__file__, "exec"),
                module.__dict__,
            )  # noqa: S102 - exact captured native-commit bytes; closed address/journal modules only
        except BaseException:
            if prior is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = prior
            raise
        modules[name] = module
    return modules["deployment_receipt"].decode_receipt


def context_for(env):
    expected = identity(env)
    folder = directory(storage(env, expected))
    value = cast(
        dict[str, Any], document(held(folder / "source.json", RECEIPT_LIMIT, private=True))
    )
    require(type(value) is dict and all(value.get(k) == v for k, v in expected.items()))
    require(
        type(value.get("native_job_id")) is int
        and value["native_job_id"] > 0
        and type(value.get("sdk_source_pins")) is dict
    )
    return value, folder


def record(context, folder, round_number, decoder):
    require(type(round_number) is int and 1 <= round_number <= MAX_ROUNDS)
    raw = held(folder / f"round-{round_number}.json", RECEIPT_LIMIT, private=True)
    document(raw)  # Reject duplicate/nonfinite JSON before the unchanged decoder.
    row = decoder(raw)
    require(
        row["operation_id"]
        == f"gh:{REPOSITORY_ID}:{context['run_id']}:{context['run_attempt']}:{round_number}"
    )
    require(row["group_population"] == [{"gseq": 1, "name": context["group"]}])
    result = {k: v for k, v in row.items() if k != "create_transaction_identifiers"}
    if "create_transaction_identifiers" in row:
        result["transaction_identifiers_digest"] = hashlib.sha256(
            canonical(row["create_transaction_identifiers"])
        ).hexdigest()
    return {
        "round": round_number,
        "native_journal_sha256": hashlib.sha256(raw).hexdigest(),
        "candidate": result,
    }


def authorize_round(env, root, round_number, getter=get):
    context, folder = context_for(env)
    require(type(round_number) is int and 1 <= round_number <= MAX_ROUNDS)
    require(native(context, env, getter) == context["native_job_id"])
    sources = sdk_capture(context, root)
    require(
        {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}
        == context["sdk_source_pins"]
    )
    require(not os.path.lexists(folder / f"round-{round_number}.json"))
    return context, sources


def round_identity(env, root, round_number, observed_dseq):
    context, folder = context_for(env)
    decoder = journal_decoder(context, root)
    row = record(context, folder, round_number, decoder)["candidate"]
    require(row["state"] == "create_response_received" and row["authority"] == "candidate")
    require(observed_dseq == "" or observed_dseq == row["dseq"])
    return row["dseq"], row["expected_owner"]


def seal(env, root):
    context, folder = context_for(env)
    decoder = journal_decoder(context, root)
    allowed = {"source.json", *(f"round-{i}.json" for i in range(1, MAX_ROUNDS + 1))}
    names = {p.name for p in folder.iterdir()}
    require(names <= allowed and "source.json" in names)
    rounds = sorted(int(n[6:-5]) for n in names if n.startswith("round-"))
    require(not rounds or rounds == list(range(1, max(rounds) + 1)))
    records = [record(context, folder, number, decoder) for number in rounds]
    report = {
        "schema": "just-akash/owned-Sentry-create-candidates/v1",
        "producer": context,
        "records": records,
        "missing_journal": not records,
        "unresolved_submission": any(
            r["candidate"]["state"] != "create_response_received" for r in records
        ),
        "close_or_replay_authority": False,
        "namespace_authority": "not-applicable-direct-Akash",
        "durability": "local-fsync-only-until-official-artifact-upload-ACK",
        "pre_upload_forced_kill_gap_qualified": False,
    }
    create(folder / "create-candidates.json", report)
    return report


def cleanup(env, uploaded):
    context, folder = context_for(env)
    require(uploaded == "success")
    held(folder / "create-candidates.json", LIMIT, private=True)
    shutil.rmtree(folder)
    fsync_directory(folder.parent)
    require(not folder.exists())


def _execute_captured(sources, root, argv):
    """Execute only original SDK CLI bytes already held by exact source admission.

    This supplies source custody, never owner/close authority. Original CLI guards
    and native caller authority remain mandatory. No installed SDK fallback exists.
    """
    require(type(argv) is list and bool(argv) and all(type(x) is str for x in argv))
    require(argv[0] in {"deploy", "resolve-owner", "destroy", "verify-closed"})
    require(set(sources) == set(SOURCE_FILES) and all(type(x) is bytes for x in sources.values()))
    modules = {
        ("just_akash" if name == "just_akash/__init__.py" else name[:-3].replace("/", ".")): raw
        for name, raw in sources.items()
        if name.startswith("just_akash/") and name.endswith(".py")
    }

    class Captured(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "just_akash" or fullname.startswith("just_akash."):
                require(fullname in modules)
                return importlib.util.spec_from_loader(
                    fullname, self, is_package=fullname == "just_akash"
                )
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            name = module.__name__
            filename = (
                "just_akash/__init__.py"
                if name == "just_akash"
                else name.replace(".", "/") + ".py"
            )
            module.__file__ = str(root / filename)
            if name == "just_akash":
                module.__path__ = []
            # Execute only the closed SDK modules already captured and source-authenticated.
            # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
            exec(compile(modules[name], module.__file__, "exec"), module.__dict__)  # noqa: S102

    old = {
        k: v
        for k, v in sys.modules.copy().items()
        if k == "just_akash" or k.startswith("just_akash.")
    }
    original_argv = sys.argv
    loader = Captured()
    try:
        for key in old:
            sys.modules.pop(key, None)
        sys.meta_path.insert(0, loader)
        sys.argv = ["just-akash", *argv]
        return importlib.import_module("just_akash.cli").main()
    finally:
        sys.argv = original_argv
        sys.meta_path.remove(loader)
        for key in list(sys.modules):
            if key == "just_akash" or key.startswith("just_akash."):
                sys.modules.pop(key, None)
        sys.modules.update(old)


def execute_cli(sdk_sha, root, argv, expected_source_pins):
    """Supported captured CLI seam for an independently authorized hosted caller.

    The caller authenticates this helper and the complete expected source pins
    before entry. This does not admit callback identity as the pool producer.
    """
    require(type(sdk_sha) is str and re.fullmatch("[0-9a-f]{40}", sdk_sha) is not None)
    require(type(expected_source_pins) is dict and set(expected_source_pins) == set(SOURCE_FILES))
    require(
        all(
            type(v) is str and re.fullmatch("[0-9a-f]{64}", v)
            for v in expected_source_pins.values()
        )
    )
    sources = sdk_capture({"sdk_sha": sdk_sha}, root)
    require(
        {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}
        == expected_source_pins
    )
    return _execute_captured(sources, root, argv)


def execute_round(env, root, round_number, argv, getter=get):
    context, sources = authorize_round(env, root, round_number, getter)
    require(bool(argv) and argv[0] == "deploy")
    return _execute_captured(sources, root, argv)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase", choices=("begin", "authorize", "round", "seal", "cleanup", "execute")
    )
    parser.add_argument("--round", type=int)
    parser.add_argument("--observed-dseq", default="")
    parser.add_argument("--upload-outcome", default="")
    args, cli_args = parser.parse_known_args()
    require(not cli_args or (args.phase == "execute" and cli_args[0] == "--"))
    if cli_args:
        cli_args = cli_args[1:]
    try:
        env = dict(os.environ)
        root = Path(__file__).resolve().parents[1]
        if args.phase == "begin":
            begin(env, root)
        elif args.phase == "authorize":
            authorize_round(env, root, args.round)
        elif args.phase == "execute":
            execute_round(env, root, args.round, cli_args)
        elif args.phase == "round":
            dseq, owner = round_identity(env, root, args.round, args.observed_dseq)
            print(dseq + " " + owner)
        elif args.phase == "seal":
            seal(env, root)
        else:
            cleanup(env, args.upload_outcome)
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        print("OWNED_CREATE_RECEIPT_HOLD_NO_RETRY", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
