from __future__ import annotations

import base64
import inspect
import textwrap
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from just_akash import chain, wallet_pool

OWNER = "akash1" + "a" * 38
DSEQ = "123"
GROUP = "runner-run-7"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
HASH_BYTES = b"h" * 32
HASH = base64.b64encode(HASH_BYTES).decode()


def _source(index: int) -> dict:
    return {
        "source_id": f"source-{index}",
        "url": f"https://lcd-{index}.example",
        "chain_id": "akashnet-2",
        "operator": f"operator-{index}",
        "gateway_ancestry": f"direct-{index}",
        "cache_ancestry": f"cache-{index}",
        "proof_mode": "height-pinned-block-id-and-paginated-group-list",
        "finality_rule": "committed-tip-minus-2",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    }


SOURCES = (_source(1), _source(2))


def _info(group: str = GROUP) -> dict:
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}},
        "groups": [
            {
                "id": {"owner": OWNER, "dseq": DSEQ, "gseq": "1"},
                "group_spec": {"name": group},
            }
        ],
    }


def _reader(
    *,
    hashes=None,
    block_time=None,
    complete_groups=(("1", GROUP),),
    reported_total=None,
    tips=None,
    block_times=None,
):
    hashes = hashes or {source["url"]: HASH for source in SOURCES}
    block_time = block_time or NOW - timedelta(seconds=10)
    tips = tips or {}
    block_times = block_times or {}
    calls = []

    def read(path, *, base, height=None):
        calls.append((base, path, height))
        if path.endswith("/latest"):
            return {
                "block": {
                    "header": {
                        "height": tips.get(base, "102"),
                        "chain_id": "akashnet-2",
                    }
                }
            }
        if "/blocks/100" in path:
            return {
                "block_id": {"hash": hashes[base]},
                "block": {
                    "header": {
                        "height": "100",
                        "chain_id": "akashnet-2",
                        "time": block_times.get(base, block_time).isoformat(),
                    }
                },
            }
        if "/deployments/info" in path:
            return _info()
        if "/groups/list" in path:
            groups = [
                {
                    "id": {"owner": OWNER, "dseq": DSEQ, "gseq": gseq},
                    "group_spec": {"name": name},
                }
                for gseq, name in complete_groups
            ]
            return {
                "groups": groups,
                "pagination": {
                    "next_key": None,
                    "total": str(len(groups) if reported_total is None else reported_total),
                },
            }
        raise AssertionError(path)

    return read, calls


def _mutated_function(function, target: str, replacement: str):
    source = inspect.getsource(function)
    assert source.count(target) == 1, "mutation target must apply exactly once"
    namespace = vars(chain).copy()
    exec(source.replace(target, replacement, 1), namespace)
    return namespace[function.__name__]


def test_fresh_same_height_block_and_exact_singleton_produce_bounded_evidence():
    reader, calls = _reader()
    evidence = chain._owner_close_evidence(
        OWNER, DSEQ, GROUP, sources=SOURCES, reader=reader, now=NOW
    )
    assert evidence is not None
    assert evidence["height"] == 100 and evidence["block_hash"] == HASH_BYTES.hex()
    assert evidence["source_ids"] == ["source-1", "source-2"]
    assert evidence["owner"] == OWNER and evidence["dseq"] == DSEQ
    assert evidence["gseq"] == "1" and evidence["group"] == GROUP
    assert evidence["population_digest"]
    assert evidence["registry_version"] == 1
    assert evidence["registry_digest"] == chain.OWNER_CORROBORATION_REGISTRY_SHA256
    assert evidence["registry_provenance_digest"] == (
        chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256
    )
    pinned = [call for call in calls if "/groups/list" in call[1] and call[2] is not None]
    assert len(pinned) == 2 and {call[2] for call in pinned} == {100}


def test_identical_truncation_is_caught_by_positive_population_total():
    reader, _ = _reader(reported_total=2)
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    target = "if len(rows) != total:\n                    return None"
    mutant = _mutated_function(
        chain._owner_close_evidence,
        target,
        "if False:\n                    return None",
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_complete_population_with_a_second_group_cannot_authorize_singleton():
    reader, _ = _reader(complete_groups=(("1", GROUP), ("2", "sidecar")))
    assert (
        chain._owner_close_evidence(OWNER, DSEQ, GROUP, sources=SOURCES, reader=reader, now=NOW)
        is None
    )


def test_returned_protocol_failure_vetoes_but_transport_failure_abstains():
    sources = (*SOURCES, _source(3))
    hashes = {source["url"]: HASH for source in sources}
    base_reader, _ = _reader(hashes=hashes)

    def reader(path, *, base, height=None):
        if base == sources[2]["url"] and "/groups/list" in path and height is not None:
            raise chain.ChainResponseError("returned data omitted the pinned height")
        return base_reader(path, base=base, height=height)

    arguments = dict(sources=sources, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    target = "except ChainResponseError:\n            return malformed"
    mutant = _mutated_function(
        chain._owner_close_evidence,
        target,
        "except ChainResponseError:\n            return None",
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None

    def unavailable(path, *, base, height=None):
        if base == sources[2]["url"] and "/groups/list" in path and height is not None:
            raise RuntimeError("transport unavailable")
        return base_reader(path, base=base, height=height)

    assert (
        chain._owner_close_evidence(
            OWNER, DSEQ, GROUP, sources=sources, reader=unavailable, now=NOW
        )
        is not None
    )


def test_missing_pinned_height_echo_is_a_protocol_failure():
    class Response:
        headers = {}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    with (
        patch.object(chain.urllib.request, "urlopen", return_value=Response()),
        pytest.raises(chain.ChainResponseError, match="did not echo pinned height"),
    ):
        chain._lcd_get("/test", base="https://lcd.example", height=100)


def test_http_response_is_a_protocol_failure_not_a_transport_abstention():
    error = chain.urllib.error.HTTPError("https://lcd.example/test", 400, "bad request", {}, None)
    with (
        patch.object(chain.urllib.request, "urlopen", side_effect=error),
        pytest.raises(chain.ChainResponseError, match="returned HTTP 400"),
    ):
        chain._lcd_get("/test", base="https://lcd.example", height=100)


def test_block_hash_disagreement_is_not_authority():
    other = base64.b64encode(b"o" * 32).decode()
    reader, _ = _reader(hashes={SOURCES[0]["url"]: HASH, SOURCES[1]["url"]: other})
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    condition = (
        "if any((item[1], item[2]) != (blocks[0][1], blocks[0][2]) for item in blocks[1:]):"
    )
    mutant = _mutated_function(
        chain._owner_close_evidence,
        condition,
        condition.replace("if any(", "if False and any("),
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_block_time_disagreement_and_tip_skew_are_not_authority():
    later = NOW - timedelta(seconds=9)
    reader, _ = _reader(block_times={SOURCES[1]["url"]: later})
    assert (
        chain._owner_close_evidence(OWNER, DSEQ, GROUP, sources=SOURCES, reader=reader, now=NOW)
        is None
    )

    reader, calls = _reader(tips={SOURCES[1]["url"]: 108})
    assert (
        chain._owner_close_evidence(OWNER, DSEQ, GROUP, sources=SOURCES, reader=reader, now=NOW)
        is None
    )
    assert not any("/blocks/100" in path for _base, path, _height in calls)


@pytest.mark.parametrize("fractional", [102.9, "102.9"])
def test_fractional_tip_height_is_malformed_and_not_coerced(fractional):
    reader, _ = _reader(tips={SOURCES[1]["url"]: fractional})
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    source = inspect.getsource(chain._owner_close_evidence)
    targets = {
        'if not isinstance(raw_height, str) or not re.fullmatch(r"[1-9][0-9]*", raw_height):': (
            "if False:"
        ),
        "height = int(raw_height)": "height = int(float(raw_height))",
    }
    for target, replacement in targets.items():
        assert source.count(target) == 1, "height mutation must apply exactly once"
        source = source.replace(target, replacement, 1)
    namespace = vars(chain).copy()
    exec(source, namespace)
    mutant = namespace["_owner_close_evidence"]
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 64,
        base64.b64encode(b"short").decode(),
        "not-base64!",
        HASH[:-2] + "h=",  # Same decoded bytes, non-canonical padding bits.
    ],
)
def test_block_hash_requires_canonical_base64_encoding_of_32_bytes(value):
    assert chain._block_hash(value) is None


def test_stale_common_block_is_not_authority():
    reader, _ = _reader(block_time=NOW - timedelta(seconds=181))
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    source = inspect.getsource(chain._owner_close_evidence)
    mutations = {
        "(now - block_time).total_seconds() > 180": (
            "(now - block_time).total_seconds() > 10_000"
        ),
        "block_time.timestamp() + 180": "block_time.timestamp() + 10_000",
    }
    for target, replacement in mutations.items():
        assert source.count(target) == 1, "freshness mutation must apply exactly once"
        source = source.replace(target, replacement, 1)
    namespace = vars(chain).copy()
    exec(source, namespace)
    mutant = namespace["_owner_close_evidence"]
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_removing_height_pin_changes_population_and_false_allows():
    base_reader, _ = _reader()
    calls = []

    def reader(path, *, base, height=None):
        calls.append((base, path, height))
        if "/groups/list" in path and height is not None:
            return {
                "groups": [
                    {
                        "id": {"owner": OWNER, "dseq": DSEQ, "gseq": "1"},
                        "group_spec": {"name": "other-group"},
                    }
                ],
                "pagination": {"next_key": None, "total": "1"},
            }
        return base_reader(path, base=base, height=height)

    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None
    assert sum(height == 100 for _base, path, height in calls if "/groups/list" in path) == 2

    target = "doc = fetch(source, page_path, height=height)"
    mutant = _mutated_function(
        chain._owner_close_evidence,
        target,
        "doc = fetch(source, page_path)",
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_expired_evidence_is_red_at_the_pre_send_gate():
    expired = {"expires_at": (NOW - timedelta(microseconds=1)).isoformat()}
    assert not wallet_pool.owner_evidence_is_unexpired(expired, now=NOW)
    assert wallet_pool.owner_evidence_is_unexpired(
        {"expires_at": (NOW + timedelta(microseconds=1)).isoformat()}, now=NOW
    )

    source = inspect.getsource(wallet_pool.owner_evidence_is_unexpired)
    target = "expiry > current"
    assert source.count(target) == 1, "mutation target must apply exactly once"
    namespace = vars(wallet_pool).copy()
    exec(source.replace(target, "True", 1), namespace)
    assert namespace["owner_evidence_is_unexpired"](expired, now=NOW)


def test_expiry_between_caller_check_and_final_send_is_red_with_effect_mutation():
    raw_client = MagicMock()
    evidence = {"dseq": DSEQ, "expires_at": (NOW + timedelta(seconds=1)).isoformat()}
    assert wallet_pool.owner_evidence_is_unexpired(evidence, now=NOW)
    closer = wallet_pool._AuthorizedBoundOwnerCloser(
        raw_client, evidence, _clock=lambda: NOW + timedelta(seconds=2)
    )
    with pytest.raises(RuntimeError, match="expired at close send boundary"):
        closer.close_deployment(DSEQ)
    raw_client.close_deployment.assert_not_called()

    source = textwrap.dedent(
        inspect.getsource(wallet_pool._AuthorizedBoundOwnerCloser.close_deployment)
    )
    target = "if not owner_evidence_is_unexpired(self.evidence, now=self._clock()):"
    assert source.count(target) == 1, "send-boundary mutation must apply exactly once"
    namespace = vars(wallet_pool).copy()
    exec(source.replace(target, "if False:", 1), namespace)
    namespace["close_deployment"](closer, DSEQ)
    raw_client.close_deployment.assert_called_once_with(DSEQ)


def _assert_destroy_authority_wiring(source: str) -> None:
    authorize = "closer, evidence = authorize_client_for_bound_owner("
    send = "closer.close_deployment(dseq)"
    assert source.count(authorize) == 1
    assert source.count(send) == 1
    assert source.index(authorize) < source.index(send)


def test_destroy_bypass_mutation_is_detected():
    import just_akash.cli as cli

    source = inspect.getsource(cli.main)
    _assert_destroy_authority_wiring(source)
    target = "closer, evidence = authorize_client_for_bound_owner("
    assert source.count(target) == 1
    mutated = source.replace(target, "closer, evidence = bypass_authority(", 1)
    try:
        _assert_destroy_authority_wiring(mutated)
    except AssertionError:
        pass
    else:
        raise AssertionError("destroy bypass mutation left wiring guard green")
