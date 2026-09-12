from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import textwrap
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from just_akash import chain, wallet_pool

OWNER = "akash1" + "a" * 38
DSEQ = "123"
GROUP = "runner-run-7"
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
CREATED_AT = 90
HASH_BYTES = b"h" * 32
HASH = base64.b64encode(HASH_BYTES).decode()
CREATE_HASH_BYTES = b"c" * 32
CREATE_HASH = base64.b64encode(CREATE_HASH_BYTES).decode()
SIGNATURE = base64.b64encode(b"signed-create").decode()


def _source(index: int) -> dict:
    return {
        "source_id": f"source-{index}",
        "url": f"https://lcd-{index}.example",
        "chain_id": "akashnet-2",
        "operator": f"operator-{index}",
        "gateway_ancestry": f"direct-{index}",
        "cache_ancestry": f"cache-{index}",
        "proof_mode": "fresh-common-height-and-complete-creation-block",
        "finality_rule": "committed-tip-minus-2",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    }


SOURCES = (_source(1), _source(2))


def _rows(groups=(("1", GROUP),)) -> list[dict]:
    return [
        {
            "id": {"owner": OWNER, "dseq": DSEQ, "gseq": gseq},
            "group_spec": {"name": name},
        }
        for gseq, name in groups
    ]


def _info(groups=(("1", GROUP),), *, created_at=CREATED_AT) -> dict:
    return {
        "deployment": {
            "id": {"owner": OWNER, "dseq": DSEQ},
            "created_at": str(created_at),
        },
        "groups": _rows(groups),
    }


def _create_tx(groups=(GROUP,)) -> dict:
    return {
        "body": {
            "messages": [
                {
                    "@type": "/akash.deployment.v1beta4.MsgCreateDeployment",
                    "id": {"owner": OWNER, "dseq": DSEQ},
                    "groups": [{"name": name} for name in groups],
                }
            ]
        },
        "auth_info": {"signer_infos": [{"sequence": "7"}]},
        "signatures": [SIGNATURE],
    }


def _other_tx() -> dict:
    return {
        "body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend"}]},
        "auth_info": {"signer_infos": [{}]},
        "signatures": [base64.b64encode(b"other-signature").decode()],
    }


def _reader(
    *,
    sources=SOURCES,
    current_groups=(("1", GROUP),),
    create_groups=(GROUP,),
    tips=None,
    current_hashes=None,
    current_block_times=None,
    create_hashes=None,
    create_block_times=None,
    created_at=CREATED_AT,
    mutate=None,
):
    tips = tips or {}
    current_hashes = current_hashes or {source["url"]: HASH for source in sources}
    current_block_times = current_block_times or {}
    create_hashes = create_hashes or {source["url"]: CREATE_HASH for source in sources}
    create_block_times = create_block_times or {}
    calls = []
    txs = [_create_tx(create_groups), _other_tx()]
    raw_txs = [base64.b64encode(f"raw-{index}".encode()).decode() for index in range(len(txs))]
    txhashes = [hashlib.sha256(base64.b64decode(raw)).hexdigest().upper() for raw in raw_txs]

    def read(path, *, base, height=None):
        calls.append((base, path, height))
        if path.endswith("/latest"):
            doc = {
                "block": {
                    "header": {
                        "height": tips.get(base, "102"),
                        "chain_id": "akashnet-2",
                    }
                }
            }
        elif "/blocks/100" in path:
            doc = {
                "block_id": {"hash": current_hashes[base]},
                "block": {
                    "header": {
                        "height": "100",
                        "chain_id": "akashnet-2",
                        "time": current_block_times.get(
                            base, NOW - timedelta(seconds=10)
                        ).isoformat(),
                    }
                },
            }
        elif "/deployments/info" in path:
            doc = _info(current_groups, created_at=created_at)
        elif f"/txs/block/{created_at}" in path:
            parsed = urllib.parse.urlsplit(path)
            query = urllib.parse.parse_qs(parsed.query)
            offset = int(query.get("pagination.offset", ["0"])[0])
            limit = int(query.get("pagination.limit", ["100"])[0])
            doc = {
                "block_id": {"hash": create_hashes[base]},
                "block": {
                    "header": {
                        "height": str(created_at),
                        "chain_id": "akashnet-2",
                        "time": create_block_times.get(base, NOW - timedelta(days=1)).isoformat(),
                    },
                    "data": {"txs": raw_txs},
                },
                "txs": txs[offset : offset + limit],
                "pagination": {"next_key": None, "total": str(len(txs))},
            }
        elif path.startswith("/cosmos/tx/v1beta1/txs?"):
            parsed = urllib.parse.urlsplit(path)
            query = urllib.parse.parse_qs(parsed.query)
            page = int(query["page"][0])
            limit = int(query["limit"][0])
            start = (page - 1) * limit
            responses = [
                {
                    "height": str(created_at),
                    "txhash": txhash,
                    "code": 0,
                }
                for txhash in txhashes
            ]
            doc = {
                "txs": txs[start : start + limit],
                "tx_responses": responses[start : start + limit],
                "pagination": None,
                "total": str(len(txs)),
            }
        else:
            raise AssertionError(path)
        doc = copy.deepcopy(doc)
        if mutate is not None:
            replacement = mutate(path, base, height, doc)
            if replacement is not None:
                doc = replacement
        return doc

    return read, calls


def _mutated_function(function, targets):
    source = inspect.getsource(function)
    if isinstance(targets, tuple) and len(targets) == 2 and isinstance(targets[0], str):
        targets = [targets]
    for target, replacement in targets:
        assert source.count(target) == 1, f"mutation target must apply exactly once: {target}"
        source = source.replace(target, replacement, 1)
    namespace = vars(chain).copy()
    exec(source, namespace)
    return namespace[function.__name__]


def test_fresh_common_height_and_complete_signed_create_produce_bounded_evidence():
    reader, calls = _reader()
    evidence = chain._owner_close_evidence(
        OWNER, DSEQ, GROUP, sources=SOURCES, reader=reader, now=NOW
    )
    assert evidence is not None
    assert evidence["height"] == 100 and evidence["block_hash"] == HASH_BYTES.hex()
    assert evidence["creation_height"] == CREATED_AT
    assert evidence["creation_block_hash"] == CREATE_HASH_BYTES.hex()
    assert len(evidence["creation_txhash"]) == 64
    assert evidence["creation_tx_index"] == 0
    assert evidence["source_ids"] == ["source-1", "source-2"]
    assert evidence["owner"] == OWNER and evidence["dseq"] == DSEQ
    assert evidence["gseq"] == "1" and evidence["group"] == GROUP
    assert evidence["population_digest"]
    assert evidence["population_count"] == 1
    assert evidence["evidence_version"] == 2
    assert evidence["registry_version"] == 2
    assert evidence["registry_digest"] == chain.OWNER_CORROBORATION_REGISTRY_SHA256
    assert evidence["registry_provenance_digest"] == (
        chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256
    )
    pinned_info = [call for call in calls if "/deployments/info" in call[1] and call[2] == 100]
    pinned_creation = [call for call in calls if "/txs" in call[1] and call[2] == CREATED_AT]
    assert len(pinned_info) == 2
    assert len(pinned_creation) == 4


def test_measured_tendermint_nanoseconds_are_accepted_but_not_weakened():
    measured = "2026-09-12T13:22:10.203273310Z"
    assert chain._rfc3339(measured) == datetime(
        2026, 9, 12, 13, 22, 10, 203273, tzinfo=timezone.utc
    )
    assert chain._rfc3339("2026-09-12T13:22:10.2032733100Z") is None
    assert chain._rfc3339("2026-09-12T13:22:10.203273310") is None

    source = inspect.getsource(chain._rfc3339)
    target = r"(\d{1,9})"
    assert source.count(target) == 1, "nanosecond normalization mutation target moved"
    namespace = vars(chain).copy()
    exec(source.replace(target, r"(\d{1,6})", 1), namespace)
    assert namespace["_rfc3339"](measured) is None, (
        "mutation applied but removing the nanosecond bound did not recreate rejection"
    )


def test_identical_truncated_current_populations_are_caught_by_signed_create_population():
    reader, _ = _reader(current_groups=(("1", GROUP),), create_groups=(GROUP, "sidecar"))
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    mutant = _mutated_function(
        chain._owner_close_evidence,
        (
            'if signed_population["snapshot"] != current_snapshot:\n            return None',
            'signed_population["snapshot"] = current_snapshot\n'
            "        if False:\n            return None",
        ),
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_complete_creation_population_with_second_group_cannot_authorize_singleton():
    reader, _ = _reader(
        current_groups=(("1", GROUP), ("2", "sidecar")),
        create_groups=(GROUP, "sidecar"),
    )
    assert (
        chain._owner_close_evidence(OWNER, DSEQ, GROUP, sources=SOURCES, reader=reader, now=NOW)
        is None
    )


def test_block_raw_population_truncation_effect_mutation():
    def truncate(_path, _base, _height, doc):
        if "/txs/block/" in _path:
            doc["block"]["data"]["txs"] = doc["block"]["data"]["txs"][:1]
        return doc

    reader, _ = _reader(mutate=truncate)
    with pytest.raises(chain.ChainResponseError, match="exact population"):
        chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    mutant = _mutated_function(
        chain._creation_block_population,
        [
            ("or len(raw_txs) != page_total", "or False"),
            ("if len(set(raw_hashes)) != page_total:", "if False:"),
        ],
    )
    result = mutant(reader, SOURCES[0], CREATED_AT)
    assert result is not None and len(result["raw_hashes"]) == 1


def test_duplicate_raw_transaction_is_rejected_with_effect_mutation():
    def duplicate(_path, _base, _height, doc):
        if "/txs/block/" in _path:
            doc["block"]["data"]["txs"][1] = doc["block"]["data"]["txs"][0]
        return doc

    reader, _ = _reader(mutate=duplicate)
    with pytest.raises(chain.ChainResponseError, match="duplicate raw transactions"):
        chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    mutant = _mutated_function(
        chain._creation_block_population,
        ("if len(set(raw_hashes)) != page_total:", "if False:"),
    )
    result = mutant(reader, SOURCES[0], CREATED_AT)
    assert result is not None and len(set(result["raw_hashes"])) == 1


def test_block_and_tx_search_paginations_are_exhausted_with_effect_mutation():
    reader, calls = _reader()
    with patch.object(chain, "_CREATION_PAGE_SIZE", 1):
        block = chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
        assert block is not None and len(block["txs"]) == 2
        signed = chain._signed_creation_population(
            reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block
        )
    assert signed is not None
    block_offsets = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["pagination.offset"][0]
        for _base, path, _height in calls
        if "/txs/block/" in path
    ]
    search_pages = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0]
        for _base, path, _height in calls
        if path.startswith("/cosmos/tx/v1beta1/txs?")
    ]
    assert block_offsets == ["0", "1"]
    assert search_pages == ["1", "2"]

    mutant = _mutated_function(
        chain._creation_block_population,
        [
            ("if offset == total:", "if True:"),
            (
                """if (
        total is None
        or len(decoded) != total
        or len(fingerprints) != total
        or len(set(fingerprints)) != total
    ):""",
                "if total is None:",
            ),
        ],
    )
    mutant.__globals__["_CREATION_PAGE_SIZE"] = 1
    collapsed = mutant(reader, SOURCES[0], CREATED_AT)
    assert collapsed is not None and len(collapsed["txs"]) == 1


def test_skipped_decode_is_caught_even_when_raw_total_is_complete():
    def skip(_path, _base, _height, doc):
        if "/txs/block/" in _path:
            # The SDK logs a decode error and omits that raw transaction. Removing the
            # first decoded row changes the ordered population while raw total stays 2.
            doc["txs"] = doc["txs"][1:]
        return doc

    reader, _ = _reader(mutate=skip)
    with pytest.raises(chain.ChainResponseError, match="decoded population was truncated"):
        chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    mutant = _mutated_function(
        chain._creation_block_population,
        [
            (
                "if len(page_txs) != expected_count:",
                "if False:",
            ),
            (
                """if (
        total is None
        or len(decoded) != total
        or len(fingerprints) != total
        or len(set(fingerprints)) != total
    ):""",
                "if total is None:",
            ),
            ("if offset == total:", "if True:"),
        ],
    )
    result = mutant(reader, SOURCES[0], CREATED_AT)
    assert result is not None
    assert len(result["fingerprints"]) == 1


def test_txhash_mismatch_effect_mutation():
    def mismatch(path, _base, _height, doc):
        if path.startswith("/cosmos/tx/v1beta1/txs?"):
            doc["tx_responses"][0]["txhash"] = "A" * 64
        return doc

    reader, _ = _reader(mutate=mismatch)
    block = chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    assert block is not None
    with pytest.raises(chain.ChainResponseError, match="did not match tx response hashes"):
        chain._signed_creation_population(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block)
    mutant = _mutated_function(
        chain._signed_creation_population,
        (
            'if tuple(response_hashes) != block_population["raw_hashes"]:',
            "if False:",
        ),
    )
    assert mutant(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block) is not None


def test_decoded_transaction_order_disagreement_effect_mutation():
    def reorder(path, _base, _height, doc):
        if path.startswith("/cosmos/tx/v1beta1/txs?"):
            doc["txs"].reverse()
        return doc

    reader, _ = _reader(mutate=reorder)
    block = chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    assert block is not None
    with pytest.raises(chain.ChainResponseError, match="decoded populations disagree"):
        chain._signed_creation_population(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block)
    mutant = _mutated_function(
        chain._signed_creation_population,
        (
            'if None in fingerprints or fingerprints != block_population["fingerprints"]:',
            "if False:",
        ),
    )
    assert mutant(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block) is not None


def test_failed_matching_create_effect_mutation():
    def fail(path, _base, _height, doc):
        if path.startswith("/cosmos/tx/v1beta1/txs?"):
            doc["tx_responses"][0]["code"] = 9
        return doc

    reader, _ = _reader(mutate=fail)
    block = chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    assert block is not None
    with pytest.raises(chain.ChainResponseError, match="not a successful signed"):
        chain._signed_creation_population(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block)
    mutant = _mutated_function(
        chain._signed_creation_population,
        ('response.get("code") != 0', "False"),
    )
    assert mutant(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block) is not None


def test_duplicate_matching_create_effect_mutation():
    def duplicate(path, _base, _height, doc):
        if "/txs/block/" in path:
            doc["txs"][1] = copy.deepcopy(doc["txs"][0])
            doc["txs"][1]["signatures"] = [base64.b64encode(b"second-signature").decode()]
            doc["txs"][1]["auth_info"]["signer_infos"][0]["sequence"] = "8"
        if path.startswith("/cosmos/tx/v1beta1/txs?"):
            doc["txs"][1] = copy.deepcopy(doc["txs"][0])
            doc["txs"][1]["signatures"] = [base64.b64encode(b"second-signature").decode()]
            doc["txs"][1]["auth_info"]["signer_infos"][0]["sequence"] = "8"
        return doc

    reader, _ = _reader(mutate=duplicate)
    block = chain._creation_block_population(reader, SOURCES[0], CREATED_AT)
    assert block is not None
    with pytest.raises(chain.ChainResponseError, match="exactly one matching create"):
        chain._signed_creation_population(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block)
    mutant = _mutated_function(
        chain._signed_creation_population,
        ("if len(matches) != 1:", "if not matches:"),
    )
    assert mutant(reader, SOURCES[0], CREATED_AT, OWNER, DSEQ, block) is not None


def test_returned_protocol_failure_vetoes_but_transport_failure_abstains():
    sources = (*SOURCES, _source(3))

    def malformed(path, base, _height, doc):
        if base == sources[2]["url"] and "/txs/block/" in path:
            raise chain.ChainResponseError("returned data omitted a row")
        return doc

    reader, _ = _reader(sources=sources, mutate=malformed)
    arguments = dict(sources=sources, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    mutant = _mutated_function(
        chain._read_source_document,
        (
            "except ChainResponseError:\n        raise",
            "except ChainResponseError:\n        return None",
        ),
    )
    with patch.object(chain, "_read_source_document", mutant):
        assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is not None

    def unavailable(path, base, _height, doc):
        if base == sources[2]["url"] and "/txs/block/" in path:
            raise RuntimeError("transport unavailable")
        return doc

    reader, _ = _reader(sources=sources, mutate=unavailable)
    assert (
        chain._owner_close_evidence(OWNER, DSEQ, GROUP, sources=sources, reader=reader, now=NOW)
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


def test_current_block_hash_disagreement_is_not_authority():
    other = base64.b64encode(b"o" * 32).decode()
    reader, _ = _reader(current_hashes={SOURCES[0]["url"]: HASH, SOURCES[1]["url"]: other})
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    condition = (
        "if any((item[1], item[2]) != (blocks[0][1], blocks[0][2]) for item in blocks[1:]):"
    )
    mutant = _mutated_function(
        chain._owner_close_evidence,
        (condition, condition.replace("if any(", "if False and any(")),
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_creation_block_hash_disagreement_is_not_authority():
    other = base64.b64encode(b"o" * 32).decode()
    reader, _ = _reader(create_hashes={SOURCES[0]["url"]: CREATE_HASH, SOURCES[1]["url"]: other})
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None
    mutant = _mutated_function(
        chain._owner_close_evidence,
        (
            "elif current_snapshot != snapshot or current_proof != creation_proof:",
            "elif current_snapshot != snapshot:",
        ),
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_creation_height_cannot_follow_the_pinned_action_height():
    reader, _ = _reader(created_at=101)
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None
    mutant = _mutated_function(
        chain._owner_close_evidence,
        ("if current_created_at > height:", "if False:"),
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_creation_block_time_cannot_follow_the_pinned_action_block_time():
    future = NOW - timedelta(seconds=5)
    reader, _ = _reader(create_block_times={source["url"]: future for source in SOURCES})
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None
    mutant = _mutated_function(
        chain._owner_close_evidence,
        ('if block_population["block_time"] > block_time:', "if False:"),
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_block_time_disagreement_and_tip_skew_are_not_authority():
    reader, _ = _reader(current_block_times={SOURCES[1]["url"]: NOW - timedelta(seconds=9)})
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

    mutant = _mutated_function(
        chain._owner_close_evidence,
        [
            (
                "if not isinstance(raw_height, str) or not "
                're.fullmatch(r"[1-9][0-9]*", raw_height):',
                "if False:",
            ),
            ("height = int(raw_height)", "height = int(float(raw_height))"),
        ],
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 64,
        base64.b64encode(b"short").decode(),
        "not-base64!",
        HASH[:-2] + "h=",
    ],
)
def test_block_hash_requires_canonical_base64_encoding_of_32_bytes(value):
    assert chain._block_hash(value) is None


def test_stale_common_block_is_not_authority():
    reader, _ = _reader(
        current_block_times={source["url"]: NOW - timedelta(seconds=181) for source in SOURCES}
    )
    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    mutant = _mutated_function(
        chain._owner_close_evidence,
        [
            (
                "(now - block_time).total_seconds() > 180",
                "(now - block_time).total_seconds() > 10_000",
            ),
            ("block_time.timestamp() + 180", "block_time.timestamp() + 10_000"),
        ],
    )
    assert mutant(OWNER, DSEQ, GROUP, **arguments) is not None


def test_removing_current_height_pin_changes_population_and_false_allows():
    base_reader, _ = _reader()

    def reader(path, *, base, height=None):
        if "/deployments/info" in path and height is not None:
            return _info((("1", "other-group"),))
        return base_reader(path, base=base, height=height)

    arguments = dict(sources=SOURCES, reader=reader, now=NOW)
    assert chain._owner_close_evidence(OWNER, DSEQ, GROUP, **arguments) is None

    mutant = _mutated_function(
        chain._owner_close_evidence,
        (
            "_read_source_document(reader, source, info_path, height=height)",
            "_read_source_document(reader, source, info_path, height=None)",
        ),
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
    with pytest.raises(AssertionError):
        _assert_destroy_authority_wiring(mutated)
