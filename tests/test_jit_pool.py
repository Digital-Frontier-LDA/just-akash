from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

from just_akash import jit_pool

IMAGE = (
    "ghcr.io/digital-frontier-lda/df-akash-runner@sha256:"
    "5b43d797d92bb081d2e085046b48579ebf0448a48fcc32f562eafa0c811b4a32"  # pragma: allowlist secret
)


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _response(payload: dict[str, object], runner_id: int, config: str) -> dict[str, object]:
    return {
        "runner": {"id": runner_id, "name": payload["name"]},
        "encoded_jit_config": config,
    }


@pytest.mark.parametrize(
    "raw",
    ["[]", '"one"', '["dup","dup"]', '["UPPER"]', '["bad_slot"]', "not-json"],
)
def test_slots_are_explicit_nonempty_unique_safe_json(raw: str) -> None:
    with pytest.raises(ValueError):
        jit_pool.parse_slots(raw)


def test_slot_contract_gives_every_slot_one_exact_routing_target() -> None:
    slots, labels, targets = jit_pool.slot_contract('["unit-1","unit-2"]', "pool-99")
    assert slots == ["unit-1", "unit-2"]
    assert labels == {"unit-1": "pool-99-unit-1", "unit-2": "pool-99-unit-2"}
    assert targets == {
        "unit-1": ["self-hosted", "linux", "akash", "pool-99-unit-1"],
        "unit-2": ["self-hosted", "linux", "akash", "pool-99-unit-2"],
    }


def test_topology_rejects_a_slot_label_over_github_limit() -> None:
    with pytest.raises(ValueError, match="100 characters"):
        jit_pool.slot_contract('["' + "a" * 32 + '"]', "x" * 79)


def test_operation_identity_binds_attempt_operation_and_group() -> None:
    deployment, label = jit_pool.operation_identity(
        "just-akash-e2epool",
        "e2epool",
        operation="7",
        run_id="12345",
        run_attempt="2",
    )
    assert deployment == (
        "just-akash-e2epool-idv2-class-ci-runner-g1-op-7-attempt-2-run-12345-end"
    )
    assert label == "e2epool-idv2-g1-op-7-attempt-2-run-12345"


@pytest.mark.parametrize("operation", ["", "0", "01", "run-1", "١"])
def test_operation_identity_rejects_noncanonical_broker_ordinal(operation: str) -> None:
    with pytest.raises(ValueError, match="create-operation"):
        jit_pool.operation_identity(
            "just-akash-e2epool",
            "e2epool",
            operation=operation,
            run_id="12345",
            run_attempt="2",
        )


def test_operation_identity_refuses_legacy_or_prestamped_prefix() -> None:
    with pytest.raises(ValueError, match="unstamped"):
        jit_pool.operation_identity(
            "just-akash-e2epool-run-123-end",
            "e2epool",
            operation="7",
            run_id="12345",
            run_attempt="2",
        )


def test_runner_name_changes_for_a_distinct_broker_operation() -> None:
    first = jit_pool._runner_name("pool", "pool-op-1", "org/repo", "123", "2", 1, "one")
    second = jit_pool._runner_name("pool", "pool-op-2", "org/repo", "123", "2", 1, "one")
    assert first.startswith("just-akash-pool-")
    assert first != second


def _bound_group() -> dict[str, object]:
    return {
        "id": 17,
        "visibility": "selected",
        "allows_public_repositories": False,
        "restricted_to_workflows": True,
        "selected_workflows": ["org/repo/.github/workflows/ci.yml@refs/heads/main"],
    }


def test_group_binding_is_single_repository_and_single_workflow() -> None:
    jit_pool.verify_group_binding(
        _bound_group(),
        [{"total_count": 1, "repositories": [{"id": 99, "full_name": "org/repo"}]}],
        group_id=17,
        repository_id=99,
        workflow_ref="org/repo/.github/workflows/ci.yml@refs/heads/main",
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"visibility": "all"}, "selected-repository"),
        ({"allows_public_repositories": True}, "refuse public"),
        ({"restricted_to_workflows": False}, "selected workflows"),
        (
            {"selected_workflows": ["org/prod/.github/workflows/deploy.yml@refs/heads/main"]},
            "calling workflow",
        ),
    ],
)
def test_group_binding_rejects_wider_authority(change: dict[str, object], message: str) -> None:
    group = _bound_group() | change
    with pytest.raises(RuntimeError, match=message):
        jit_pool.verify_group_binding(
            group,
            [{"total_count": 1, "repositories": [{"id": 99}]}],
            group_id=17,
            repository_id=99,
            workflow_ref="org/repo/.github/workflows/ci.yml@refs/heads/main",
        )


def test_group_binding_rejects_an_extra_repository_even_when_caller_is_present() -> None:
    with pytest.raises(RuntimeError, match="only to the calling repository"):
        jit_pool.verify_group_binding(
            _bound_group(),
            [{"total_count": 2, "repositories": [{"id": 99}, {"id": 100}]}],
            group_id=17,
            repository_id=99,
            workflow_ref="org/repo/.github/workflows/ci.yml@refs/heads/main",
        )


def test_group_binding_rejects_a_truncated_repository_population() -> None:
    with pytest.raises(RuntimeError, match="truncated"):
        jit_pool.verify_group_binding(
            _bound_group(),
            [{"total_count": 2, "repositories": [{"id": 99}]}],
            group_id=17,
            repository_id=99,
            workflow_ref="org/repo/.github/workflows/ci.yml@refs/heads/main",
        )


def test_prepare_journals_each_exact_identity_before_requesting_the_next(tmp_path: Path) -> None:
    parent = _private(tmp_path / "private")
    journal = parent / "identities.json"
    seen: list[dict[str, object]] = []

    def generate(_org: str, payload: dict[str, object]) -> dict[str, object]:
        if seen:
            durable = json.loads(journal.read_text())
            assert durable["group_id"] == 17
            assert durable["runners"][0]["id"] == 101
            assert durable["runners"][0]["name"] == seen[0]["name"]
            assert durable["runners"][0]["slot"] == "unit-1"
            assert "pool-99-unit-1" in durable["runners"][0]["labels"]
        seen.append(payload)
        return _response(payload, 100 + len(seen), f"config-{len(seen)}")

    identities = jit_pool.prepare_attempt(
        org="example",
        group_id=17,
        raw_slots='["unit-1","unit-2"]',
        runner_label="pool-99",
        operation_label="pool-99",
        repository="org/repo",
        run_id="123",
        run_attempt="2",
        provider_attempt=1,
        image=IMAGE,
        placement="repo-run-123-end",
        cpu="4",
        memory="16Gi",
        storage="30Gi",
        journal_path=journal,
        sdl_path=parent / "pool.yaml",
        generate=generate,
        delete=lambda _org, _runner_id: None,
    )

    assert [item.runner_id for item in identities] == [101, 102]
    assert all(payload["runner_group_id"] == 17 for payload in seen)
    assert all(str(payload["name"]).startswith("just-akash-pool-99-") for payload in seen)
    first_labels = seen[0]["labels"]
    second_labels = seen[1]["labels"]
    assert isinstance(first_labels, list) and first_labels[-1] == "pool-99-unit-1"
    assert isinstance(second_labels, list) and second_labels[-1] == "pool-99-unit-2"
    document = yaml.safe_load((parent / "pool.yaml").read_text())
    assert len(document["services"]) == 2
    assert all(
        spec["count"] == 1
        for service in document["deployment"].values()
        for spec in service.values()
    )
    envs = [entry for service in document["services"].values() for entry in service["env"]]
    configs = [entry for entry in envs if entry.startswith("RUNNER_JIT_CONFIG=")]
    assert configs == ["RUNNER_JIT_CONFIG=config-1", "RUNNER_JIT_CONFIG=config-2"]
    assert not any(entry.startswith(("ACCESS_TOKEN=", "RUNNER_TOKEN=")) for entry in envs)
    assert "config-1" not in journal.read_text()
    assert "config-2" not in journal.read_text()


def _journal_runner(runner_id: int, name: str, slot_label: str) -> dict[str, object]:
    return {
        "id": runner_id,
        "name": name,
        "slot": slot_label.rsplit("-", 1)[-1],
        "labels": ["self-hosted", "linux", "akash", "pool-99", slot_label],
    }


def _listed_runner(runner_id: int, name: str, slot_label: str) -> dict[str, object]:
    return {
        "id": runner_id,
        "name": name,
        "status": "online",
        "busy": False,
        "version": "2.999.0",
        "labels": [
            {"name": label, "type": "custom"}
            for label in ("self-hosted", "linux", "akash", "pool-99", slot_label)
        ],
    }


def test_group_population_binds_every_exact_id_name_label_across_all_pages() -> None:
    journal = {
        "group_id": 17,
        "runner_label": "pool-99",
        "operation_label": "pool-99",
        "runners": [
            _journal_runner(101, "runner-one", "pool-99-unit-1"),
            _journal_runner(102, "runner-two", "pool-99-unit-2"),
        ],
    }
    result = jit_pool.observe_group_population(
        journal,
        [
            {"total_count": 2, "runners": [_listed_runner(101, "runner-one", "pool-99-unit-1")]},
            {"total_count": 2, "runners": [_listed_runner(102, "runner-two", "pool-99-unit-2")]},
        ],
    )
    assert result == {
        "group_id": 17,
        "expected": 2,
        "online": 2,
        "online_ids": [101, 102],
        "versions": ["2.999.0", "2.999.0"],
    }


@pytest.mark.parametrize("field", ["name", "labels"])
def test_group_population_rejects_identity_or_routing_drift(field: str) -> None:
    journal = {
        "group_id": 17,
        "runner_label": "pool-99",
        "operation_label": "pool-99",
        "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
    }
    observed = _listed_runner(101, "runner-one", "pool-99-unit-1")
    observed[field] = "runner-other" if field == "name" else [{"name": "self-hosted"}]
    with pytest.raises(RuntimeError):
        jit_pool.observe_group_population(journal, [{"total_count": 1, "runners": [observed]}])


def test_live_population_read_uses_the_exact_journaled_group_and_paginates(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "journal_type": "just-akash/jit-pool-attempt/v1",
                "org": "example",
                "group_id": 17,
                "runner_label": "pool-99",
                "operation_label": "pool-99",
                "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
            }
        )
    )
    calls: list[tuple[str, bool]] = []

    def read(path: str, *, paginate: bool = False) -> list[object]:
        calls.append((path, paginate))
        return [
            {"total_count": 1, "runners": [_listed_runner(101, "runner-one", "pool-99-unit-1")]}
        ]

    result = jit_pool.observe_live_group_population(journal, read=read)
    assert result["online"] == 1
    assert calls == [("orgs/example/actions/runner-groups/17/runners?per_page=100", True)]


def test_group_population_rejects_an_extra_same_operation_runner() -> None:
    journal = {
        "group_id": 17,
        "runner_label": "pool-99",
        "operation_label": "pool-99",
        "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
    }
    expected = _listed_runner(101, "runner-one", "pool-99-unit-1")
    extra = _listed_runner(999, "runner-extra", "pool-99-unit-9")
    with pytest.raises(RuntimeError, match="unjournaled operation runner"):
        jit_pool.observe_group_population(
            journal, [{"total_count": 2, "runners": [expected, extra]}]
        )


def test_group_population_rejects_a_truncated_runner_page() -> None:
    journal = {
        "group_id": 17,
        "runner_label": "pool-99",
        "operation_label": "pool-99",
        "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
    }
    runner = _listed_runner(101, "runner-one", "pool-99-unit-1")
    with pytest.raises(RuntimeError, match="truncated"):
        jit_pool.observe_group_population(
            journal,
            [{"total_count": 2, "runners": [runner]}],
        )


@pytest.mark.parametrize("case", ["repository-policy", "runner-population"])
def test_total_count_effect_mutation_changes_both_population_verdicts(case: str) -> None:
    source = Path(jit_pool.__file__).read_text(encoding="utf-8")
    target = "    if expected_total is None or len(entries) != expected_total:\n"
    assert source.count(target) == 1
    mutant = source.replace(target, "    if expected_total is None:\n")
    module = types.ModuleType(f"jit_pool_total_{case}_mutant")
    module.__file__ = str(jit_pool.__file__)
    sys.modules[module.__name__] = module
    exec(compile(mutant, str(jit_pool.__file__), "exec"), module.__dict__)
    if case == "repository-policy":
        module.verify_group_binding(
            _bound_group(),
            [{"total_count": 2, "repositories": [{"id": 99}]}],
            group_id=17,
            repository_id=99,
            workflow_ref="org/repo/.github/workflows/ci.yml@refs/heads/main",
        )
    else:
        journal = {
            "group_id": 17,
            "runner_label": "pool-99",
            "operation_label": "pool-99",
            "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
        }
        runner = _listed_runner(101, "runner-one", "pool-99-unit-1")
        result = module.observe_group_population(
            journal,
            [{"total_count": 2, "runners": [runner]}],
        )
        assert result["online"] == 1


def test_busy_runner_does_not_satisfy_the_ready_population() -> None:
    journal = {
        "group_id": 17,
        "runner_label": "pool-99",
        "operation_label": "pool-99",
        "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
    }
    runner = _listed_runner(101, "runner-one", "pool-99-unit-1")
    runner["busy"] = True
    result = jit_pool.observe_group_population(journal, [{"total_count": 1, "runners": [runner]}])
    assert result["online"] == 0


@pytest.mark.parametrize(
    ("target", "replacement", "case"),
    [
        (
            "            if operation_label in actual_labels:\n",
            "            if False:\n",
            "extra",
        ),
        (
            '        and observed[runner_id].get("busy") is False\n',
            "        and True\n",
            "busy",
        ),
    ],
)
def test_population_effect_mutations_apply_and_change_the_verdict(
    target: str,
    replacement: str,
    case: str,
) -> None:
    source = Path(jit_pool.__file__).read_text(encoding="utf-8")
    assert source.count(target) == 1
    mutant = source.replace(target, replacement)
    module = types.ModuleType(f"jit_pool_population_{case}_mutant")
    module.__file__ = str(jit_pool.__file__)
    sys.modules[module.__name__] = module
    exec(compile(mutant, str(jit_pool.__file__), "exec"), module.__dict__)
    journal = {
        "group_id": 17,
        "runner_label": "pool-99",
        "operation_label": "pool-99",
        "runners": [_journal_runner(101, "runner-one", "pool-99-unit-1")],
    }
    expected = _listed_runner(101, "runner-one", "pool-99-unit-1")
    if case == "extra":
        extra = _listed_runner(999, "runner-extra", "pool-99-unit-9")
        result = module.observe_group_population(
            journal, [{"total_count": 2, "runners": [expected, extra]}]
        )
        assert result["online"] == 1
    else:
        expected["busy"] = True
        result = module.observe_group_population(
            journal, [{"total_count": 1, "runners": [expected]}]
        )
        assert result["online"] == 1


def test_generate_group_call_site_mutation_changes_the_observed_authority(tmp_path: Path) -> None:
    source = Path(jit_pool.__file__).read_text(encoding="utf-8")
    target = '                    "runner_group_id": group_id,\n'
    assert source.count(target) == 1
    mutant = source.replace(target, '                    "runner_group_id": group_id + 1,\n')
    module = types.ModuleType("jit_pool_group_mutant")
    module.__file__ = str(jit_pool.__file__)
    sys.modules[module.__name__] = module
    exec(compile(mutant, str(jit_pool.__file__), "exec"), module.__dict__)
    seen: list[int] = []

    def generate(_org: str, payload: dict[str, object]) -> dict[str, object]:
        group_id = payload["runner_group_id"]
        assert isinstance(group_id, int)
        seen.append(group_id)
        return _response(payload, 101, "config")

    parent = _private(tmp_path / "private")
    module.prepare_attempt(
        org="example",
        group_id=17,
        raw_slots='["one"]',
        runner_label="pool",
        operation_label="pool",
        repository="org/repo",
        run_id="123",
        run_attempt="1",
        provider_attempt=1,
        image=IMAGE,
        placement="repo-run-123-end",
        cpu="4",
        memory="16Gi",
        storage="30Gi",
        journal_path=parent / "identities.json",
        sdl_path=parent / "pool.yaml",
        generate=generate,
        delete=lambda _org, _runner_id: None,
    )
    assert seen == [18], "the exact generate-jitconfig group authority did not mutate"


def test_partial_generation_removes_every_created_identity_by_exact_id(tmp_path: Path) -> None:
    parent = _private(tmp_path / "private")
    deleted: list[tuple[str, int]] = []
    calls = 0

    def generate(_org: str, payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("API stopped")
        return _response(payload, 321, "one-use")

    with pytest.raises(RuntimeError, match="API stopped"):
        jit_pool.prepare_attempt(
            org="example",
            group_id=17,
            raw_slots='["one","two"]',
            runner_label="pool",
            operation_label="pool",
            repository="org/repo",
            run_id="123",
            run_attempt="1",
            provider_attempt=1,
            image=IMAGE,
            placement="repo-run-123-end",
            cpu="4",
            memory="16Gi",
            storage="30Gi",
            journal_path=parent / "identities.json",
            sdl_path=parent / "pool.yaml",
            generate=generate,
            delete=lambda org, runner_id: deleted.append((org, runner_id)),
        )
    assert deleted == [("example", 321)]
    assert json.loads((parent / "identities.json").read_text())["runners"] == []


def test_exact_cleanup_accepts_an_already_absent_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jit_pool, "_gh_executable", lambda: "/usr/bin/gh")
    monkeypatch.setattr(
        jit_pool.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="gh: Not Found (HTTP 404)",
        ),
    )
    jit_pool._gh_delete("example", 321)


def test_malformed_post_create_response_is_still_removed_by_returned_id(tmp_path: Path) -> None:
    parent = _private(tmp_path / "private")
    deleted: list[int] = []

    def wrong_name(_org: str, payload: dict[str, object]) -> dict[str, object]:
        return {"runner": {"id": 987, "name": "not-the-requested-name"}, "encoded_jit_config": "x"}

    with pytest.raises(RuntimeError, match="name did not match"):
        jit_pool.prepare_attempt(
            org="example",
            group_id=17,
            raw_slots='["one"]',
            runner_label="pool",
            operation_label="pool",
            repository="org/repo",
            run_id="123",
            run_attempt="1",
            provider_attempt=1,
            image=IMAGE,
            placement="repo-run-123-end",
            cpu="4",
            memory="16Gi",
            storage="30Gi",
            journal_path=parent / "identities.json",
            sdl_path=parent / "pool.yaml",
            generate=wrong_name,
            delete=lambda _org, runner_id: deleted.append(runner_id),
        )
    assert deleted == [987]


def test_cleanup_call_site_mutation_leaks_and_is_observed(tmp_path: Path) -> None:
    source = Path(jit_pool.__file__).read_text(encoding="utf-8")
    target = "        cleanup_attempt(journal_path, delete=delete)\n"
    assert source.count(target) == 1
    mutant = source.replace(target, "        pass  # cleanup call-site mutation\n")
    module = types.ModuleType("jit_pool_mutant")
    module.__file__ = str(jit_pool.__file__)
    sys.modules[module.__name__] = module
    exec(compile(mutant, str(jit_pool.__file__), "exec"), module.__dict__)
    parent = _private(tmp_path / "private")
    deleted: list[int] = []
    calls = 0

    def generate(_org: str, payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("effect mutation")
        return _response(payload, 654, "fresh")

    with pytest.raises(RuntimeError, match="effect mutation"):
        module.prepare_attempt(
            org="example",
            group_id=17,
            raw_slots='["one","two"]',
            runner_label="pool",
            operation_label="pool",
            repository="org/repo",
            run_id="123",
            run_attempt="1",
            provider_attempt=1,
            image=IMAGE,
            placement="repo-run-123-end",
            cpu="4",
            memory="16Gi",
            storage="30Gi",
            journal_path=parent / "identities.json",
            sdl_path=parent / "pool.yaml",
            generate=generate,
            delete=lambda _org, runner_id: deleted.append(runner_id),
        )
    assert deleted != [654], "deleting the cleanup call site must change the observed effect"
