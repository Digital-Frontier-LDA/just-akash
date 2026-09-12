"""Two independent chain sources, never Console alone, authorize bound-owner close."""

from __future__ import annotations

import inspect
import re
import urllib.parse
from unittest.mock import MagicMock

import pytest

from just_akash import chain, wallet_pool

OWNER = "akash1" + "a" * 38
DSEQ = "123"
ENDPOINTS = ("https://lcd-a.example", "https://lcd-b.example")


def _source(url: str, index: int) -> dict:
    return {
        "source_id": f"source-{index}",
        "url": url,
        "chain_id": "akashnet-2",
        "operator": f"operator-{index}",
        "gateway_ancestry": f"direct-{index}",
        "cache_ancestry": f"cache-{index}",
        "proof_mode": "fresh-common-height-and-complete-creation-block",
        "finality_rule": "committed-tip-minus-2",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    }


SOURCES = tuple(_source(url, index) for index, url in enumerate(ENDPOINTS))


def _info(*groups: tuple[int, str], owner: str = OWNER, dseq: str = DSEQ) -> dict:
    return {
        "deployment": {"id": {"owner": owner, "dseq": dseq}},
        "groups": [
            {
                "id": {"owner": owner, "dseq": dseq, "gseq": gseq},
                "group_spec": {"name": name},
            }
            for gseq, name in groups
        ],
    }


def _reader_for(payloads: dict[str, object]):
    def read(_path: str, *, base: str):
        answer = payloads[base]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return read


def test_two_independent_sources_agree_on_every_group_and_identity():
    # Response order is not identity; gseq is. Both complete populations agree.
    reader = _reader_for(
        {
            ENDPOINTS[0]: _info((1, "runner"), (2, "sidecar")),
            ENDPOINTS[1]: _info((2, "sidecar"), (1, "runner")),
        }
    )

    assert chain._corroborated_deployment_group_names(
        OWNER, DSEQ, sources=SOURCES, reader=reader
    ) == ["runner", "sidecar"]


def test_one_source_cannot_authorize_bound_owner_teardown():
    reader = _reader_for({ENDPOINTS[0]: _info((1, "runner"))})

    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=SOURCES[:1], reader=reader)
        == []
    )


def test_two_urls_on_one_hostname_are_one_source():
    endpoints = ("https://lcd.example:443/a", "https://LCD.EXAMPLE:8443/b")
    reader = _reader_for({endpoint: _info((1, "runner")) for endpoint in endpoints})

    assert (
        chain._corroborated_deployment_group_names(
            OWNER,
            DSEQ,
            sources=tuple(_source(u, i) for i, u in enumerate(endpoints)),
            reader=reader,
        )
        == []
    )


@pytest.mark.parametrize("field", ["gateway_ancestry", "cache_ancestry"])
def test_different_hosts_sharing_gateway_or_cache_ancestry_are_one_trust_path(field):
    sources = (
        {**_source(ENDPOINTS[0], 0), field: "shared"},
        {**_source(ENDPOINTS[1], 1), field: "shared"},
    )
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )


def test_duplicate_source_identity_is_one_trust_path():
    sources = (
        _source(ENDPOINTS[0], 0),
        {**_source(ENDPOINTS[1], 1), "source_id": "source-0"},
    )
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )


@pytest.mark.parametrize(
    "missing",
    [
        "source_id",
        "chain_id",
        "operator",
        "gateway_ancestry",
        "cache_ancestry",
        "proof_mode",
        "finality_rule",
        "max_age_seconds",
        "max_height_skew",
    ],
)
def test_missing_trust_registry_metadata_cannot_vote(missing):
    sources = [dict(source) for source in SOURCES]
    sources[0][missing] = ""
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources[:2], reader=reader)
        == []
    )


def test_a_dead_source_can_be_replaced_but_cannot_vote():
    endpoints = ("https://dead.example", *ENDPOINTS)
    reader = _reader_for(
        {
            endpoints[0]: RuntimeError("unavailable"),
            ENDPOINTS[0]: _info((1, "runner")),
            ENDPOINTS[1]: _info((1, "runner")),
        }
    )

    assert chain._corroborated_deployment_group_names(
        OWNER,
        DSEQ,
        sources=tuple(_source(u, i) for i, u in enumerate(endpoints)),
        reader=reader,
    ) == ["runner"]


def test_every_readable_source_must_agree_not_just_the_first_two():
    endpoints = (*ENDPOINTS, "https://lcd-c.example")
    reader = _reader_for(
        {
            ENDPOINTS[0]: _info((1, "runner")),
            ENDPOINTS[1]: _info((1, "runner")),
            endpoints[2]: _info((1, "other")),
        }
    )
    sources = tuple(_source(u, i) for i, u in enumerate(endpoints))
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )


def test_effect_mutation_skipping_a_malformed_third_response_false_allows():
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "if snapshot is None:\n            return []"
    assert source.count(target) == 1
    mutant = _mutated_consensus(
        source.replace(target, "if snapshot is None:\n            continue", 1)
    )
    endpoints = (*ENDPOINTS, "https://lcd-c.example")
    sources = tuple(_source(url, index) for index, url in enumerate(endpoints))
    reader = _reader_for(
        {
            ENDPOINTS[0]: _info((1, "runner")),
            ENDPOINTS[1]: _info((1, "runner")),
            endpoints[2]: {},
        }
    )
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=sources, reader=reader) == ["runner"]


def test_default_runtime_sources_are_exactly_two_eligible_destructive_voters(monkeypatch):
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    sources = chain.OWNER_CORROBORATION_SOURCES_V2
    hosts = {urllib.parse.urlsplit(source["url"]).hostname for source in sources}

    assert len(sources) == len(hosts) == 2
    assert {s["source_id"] for s in sources} == {"quad", "c29r3"}
    assert all("pocket" not in s["source_id"] for s in sources)
    assert len({s["operator"] for s in sources}) == 2
    assert len({s["gateway_ancestry"] for s in sources}) == 2
    assert len({s["cache_ancestry"] for s in sources}) == 2
    assert {s["chain_id"] for s in sources} == {"akashnet-2"}
    assert chain.OWNER_CORROBORATION_REGISTRY_VERSION == 2
    assert "akash/chain.json" in chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE
    assert "c67c94a5f5c41ad1b116b1b847ef8b5f196b6405" in (
        chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE
    )
    assert len(chain.OWNER_CORROBORATION_REGISTRY_SHA256) == 64
    assert len(chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256) == 64


def test_registry_population_cannot_silently_become_empty_or_one(monkeypatch):
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    monkeypatch.setattr(chain, "_lcd_get", lambda _path, *, base: _info((1, "runner")))
    for sources in ((), chain.OWNER_CORROBORATION_SOURCES_V2[:1]):
        monkeypatch.setattr(chain, "OWNER_CORROBORATION_SOURCES_V2", sources)
        monkeypatch.setattr(
            chain, "OWNER_CORROBORATION_REGISTRY_SHA256", chain._source_registry_digest(sources)
        )
        assert chain.corroborated_deployment_group_names(OWNER, DSEQ, "runner") == []
        assert chain.owner_close_evidence(OWNER, DSEQ, "runner") is None


@pytest.mark.parametrize("gseq", [2, "02"])
def test_public_containment_requires_the_exact_gseq_one_singleton(monkeypatch, gseq):
    reader = _reader_for({url: _info((gseq, "runner")) for url in ENDPOINTS})
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    monkeypatch.setattr(chain, "OWNER_CORROBORATION_SOURCES_V2", SOURCES)
    monkeypatch.setattr(
        chain, "OWNER_CORROBORATION_REGISTRY_SHA256", chain._source_registry_digest(SOURCES)
    )
    monkeypatch.setattr(chain, "_lcd_get", reader)
    assert chain.corroborated_deployment_group_names(OWNER, DSEQ, "runner") == []


def test_public_containment_accepts_only_exact_expected_singleton(monkeypatch):
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    monkeypatch.setattr(chain, "OWNER_CORROBORATION_SOURCES_V2", SOURCES)
    monkeypatch.setattr(
        chain, "OWNER_CORROBORATION_REGISTRY_SHA256", chain._source_registry_digest(SOURCES)
    )
    monkeypatch.setattr(chain, "_lcd_get", reader)
    assert chain.corroborated_deployment_group_names(OWNER, DSEQ, "runner") == ["runner"]
    assert chain.corroborated_deployment_group_names(OWNER, DSEQ, "other") == []


@pytest.mark.parametrize(
    ("label", "second"),
    [
        ("truncated group population", _info((1, "runner"))),
        ("different group name", _info((1, "runner"), (2, "other"))),
        (
            "wrong deployment owner",
            _info((1, "runner"), (2, "sidecar"), owner="akash1" + "b" * 38),
        ),
        ("wrong deployment dseq", _info((1, "runner"), (2, "sidecar"), dseq="999")),
    ],
)
def test_disagreement_or_truncation_is_unknown(label, second):
    reader = _reader_for(
        {
            ENDPOINTS[0]: _info((1, "runner"), (2, "sidecar")),
            ENDPOINTS[1]: second,
        }
    )

    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=SOURCES, reader=reader)
        == []
    ), label


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(groups=[doc["groups"][0], {}]),
        lambda doc: doc["groups"][1].update(id={"owner": OWNER, "dseq": DSEQ}),
        lambda doc: doc["groups"][1]["id"].update(owner="akash1" + "b" * 38),
        lambda doc: doc["groups"][1]["id"].update(dseq="999"),
        lambda doc: doc["groups"][1]["id"].update(gseq=1),
        lambda doc: doc["groups"][1]["id"].update(gseq=0),
        lambda doc: doc["groups"][1]["id"].update(gseq=-1),
        lambda doc: doc["groups"][1]["id"].update(gseq="٢"),
        lambda doc: doc["groups"][1]["id"].update(gseq="1" * 33),
        lambda doc: doc["groups"][1]["group_spec"].update(name=""),
        lambda doc: doc.update(pagination={"next_key": "more"}),
    ],
)
def test_malformed_sibling_invalidates_the_whole_population(mutate):
    malformed = _info((1, "runner"), (2, "sidecar"))
    mutate(malformed)
    reader = _reader_for(
        {
            ENDPOINTS[0]: _info((1, "runner"), (2, "sidecar")),
            ENDPOINTS[1]: malformed,
        }
    )

    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=SOURCES, reader=reader)
        == []
    )


def _mutated_consensus(source: str):
    namespace = {
        "_DEPLOYMENT_API": "/akash/deployment/v1beta4",
        "_deployment_group_snapshot": chain._deployment_group_snapshot,
        "_lcd_get": chain._lcd_get,
        "rest_urls": chain.rest_urls,
        "re": re,
        "urllib": urllib,
    }
    exec("from __future__ import annotations\n" + source, namespace)  # noqa: S102
    return namespace["_corroborated_deployment_group_names"]


def test_effect_mutation_single_source_acceptance_changes_the_verdict():
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "if len(snapshots) < 2:"
    assert source.count(target) == 1, "consensus mutation target must apply exactly once"
    replacement = target.replace("< 2", "< 1")
    mutant = _mutated_consensus(source.replace(target, replacement, 1))
    reader = _reader_for({ENDPOINTS[0]: _info((1, "runner"))})

    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=SOURCES[:1], reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=SOURCES[:1], reader=reader) == ["runner"]


@pytest.mark.parametrize(
    "second",
    [
        _info((1, "runner")),
        _info((1, "runner"), (2, "other")),
    ],
    ids=["truncated-population", "different-group"],
)
def test_effect_mutation_ignoring_disagreement_authorizes_the_first_population(second):
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "if any(snapshot != snapshots[0] for snapshot in snapshots[1:]):"
    assert source.count(target) == 1, "disagreement mutation target must apply exactly once"
    mutant = _mutated_consensus(source.replace(target, "if False:", 1))
    reader = _reader_for(
        {
            ENDPOINTS[0]: _info((1, "runner"), (2, "sidecar")),
            ENDPOINTS[1]: second,
        }
    )

    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=SOURCES, reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=SOURCES, reader=reader) == ["runner", "sidecar"]


def test_effect_mutation_counting_shared_ancestry_reopens_false_quorum():
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "or ancestry in ancestries"
    assert source.count(target) == 1
    mutant = _mutated_consensus(source.replace(target, "or False", 1))
    sources = (
        {**_source(ENDPOINTS[0], 0), "gateway_ancestry": "shared"},
        {**_source(ENDPOINTS[1], 1), "gateway_ancestry": "shared"},
    )
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=sources, reader=reader) == ["runner"]


def test_effect_mutation_counting_shared_cache_reopens_false_quorum():
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "or cache_ancestry in cache_ancestries"
    assert source.count(target) == 1
    mutant = _mutated_consensus(source.replace(target, "or False", 1))
    sources = (
        {**_source(ENDPOINTS[0], 0), "cache_ancestry": "shared"},
        {**_source(ENDPOINTS[1], 1), "cache_ancestry": "shared"},
    )
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=sources, reader=reader) == ["runner"]


def test_effect_mutation_counting_duplicate_source_id_reopens_false_quorum():
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "source_id in source_ids"
    assert source.count(target) == 1
    mutant = _mutated_consensus(source.replace(target, "False", 1))
    sources = (
        _source(ENDPOINTS[0], 0),
        {**_source(ENDPOINTS[1], 1), "source_id": "source-0"},
    )
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=sources, reader=reader) == ["runner"]


def test_effect_mutation_accepting_missing_registry_metadata_reopens_false_quorum():
    source = inspect.getsource(chain._corroborated_deployment_group_names)
    target = "identifiers = (source_id, operator, ancestry, cache_ancestry)"
    assert source.count(target) == 1
    mutant = _mutated_consensus(
        source.replace(
            target,
            "identifiers = (source_id, ancestry, cache_ancestry)",
            1,
        )
    )
    sources = [dict(source) for source in SOURCES[:2]]
    sources[0]["operator"] = ""
    reader = _reader_for({url: _info((1, "runner")) for url in ENDPOINTS})
    assert (
        chain._corroborated_deployment_group_names(OWNER, DSEQ, sources=sources, reader=reader)
        == []
    )
    assert mutant(OWNER, DSEQ, sources=sources, reader=reader) == ["runner"]


def _mutated_bound_owner(source: str):
    namespace = {
        "AkashConsoleAPI": object,
        "Callable": object,
        "chain": chain,
        "configured_api_keys": lambda: ["owner-key"],
        "re": re,
    }
    exec("from __future__ import annotations\n" + source, namespace)  # noqa: S102
    return namespace["select_client_for_bound_owner"]


def test_production_containment_reader_has_no_source_injection():
    signature = inspect.signature(chain.corroborated_deployment_group_names)
    assert tuple(signature.parameters) == ("owner", "dseq", "expected_group")


def test_bound_owner_destructive_selection_holds_after_positive_containment(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEY", "owner-key")
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    client = MagicMock()
    client.account_address.return_value = OWNER
    evidence = MagicMock(return_value=["runner"])
    monkeypatch.setattr(chain, "corroborated_deployment_group_names", evidence)
    monkeypatch.setattr(chain, "owner_close_evidence", lambda *_args: None)
    with pytest.raises(RuntimeError, match="not fresh/finalized"):
        wallet_pool.authorize_client_for_bound_owner(
            DSEQ, OWNER, "runner", client_factory=lambda _key: client
        )
    evidence.assert_called_once_with(OWNER, DSEQ, "runner")
    client.close_deployment.assert_not_called()
