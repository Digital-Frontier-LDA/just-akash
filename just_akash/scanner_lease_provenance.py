"""Optional hosted scanner-v2 hooks at the actual SDK deployment/lease boundaries.

No callback or close/delete permission is conferred by these candidate records.
A durable recorder and fresh native admission are mandatory; neither is synthesized.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml

from .deployment_receipt import PreparedDeploymentReceipt
from .scanner_provenance import (
    IMAGE,
    PROVIDERS,
    DurableAck,
    DurableRecorder,
    ScannerBinding,
    ScannerOperation,
    ScannerProvenanceHeld,
    positive,
    record_sha256,
    require,
    validate_ack,
    validate_record,
)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        require(type(key) is str and key not in result)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _receipt_secret_views(config: str, password: str) -> tuple[bytes, ...] | None:
    """Memory-only credential views; never copy them or their hashes to receipts."""
    views = [config.encode(), password.encode()]
    try:
        decoded = base64.b64decode(config, validate=True)
    except ValueError:
        return tuple(views)
    views.append(decoded)
    try:
        files = json.loads(decoded)
    except (ValueError, UnicodeError):
        return tuple(views)
    if type(files) is dict:
        for value in files.values():
            if type(value) is str:
                views.append(value.encode())
                try:
                    payload = base64.b64decode(value, validate=True)
                except ValueError:
                    continue
                views.append(payload)
                try:
                    pending = [json.loads(payload)]
                except (ValueError, UnicodeError):
                    continue
                except RecursionError:
                    # Optional transaction metadata is withheld if credential
                    # strings cannot be completely inspected; DSEQ still survives.
                    return None
                while pending:
                    item = pending.pop()
                    if type(item) is str:
                        views.append(item.encode())
                    elif type(item) is dict:
                        pending.extend(item.values())
                    elif type(item) is list:
                        pending.extend(item)
    return tuple(views)


def public_sdl_request(content: str, binding: ScannerBinding, config: str) -> dict[str, object]:
    """Validate exact tenant payload; replace secrets before any public commitment."""
    require(type(content) is str and 0 < len(content.encode()) <= 65536)
    doc = None
    try:
        # No aliases, tags, duplicate fields or attacker-selected object constructors.
        require(not any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(content)))
        doc = yaml.load(content, Loader=_UniqueLoader)  # noqa: S506 -- subclass of SafeLoader only
    except Exception:
        doc = None
    require(type(doc) is dict)
    doc = cast(dict[str, Any], doc)
    require(type(doc.get("services")) is dict and type(doc["services"].get("runner")) is dict)
    service = cast(dict[str, Any], doc["services"]["runner"])
    credentials = service.get("credentials", {})
    require(type(credentials) is dict and set(credentials) == {"host", "username", "password"})
    require(
        credentials["host"] == "https://index.docker.io/v1/"
        and credentials["username"] == "jobordu"
    )
    require(
        type(credentials["password"]) is str
        and 0 < len(cast(str, credentials["password"])) <= 8192
    )
    env = service.get("env")
    require(type(env) is list and len(env) == 4 and all(type(value) is str for value in env))
    env = cast(list[str], env)
    expected_env = {
        "RUNNER_JIT_CONFIG": config,
        "EPHEMERAL": "true",
        "RUNNER_EXECUTION_USER": "root",
        "RUNNER_NAME": binding.runner_name,
    }
    require(len({value.split("=", 1)[0] for value in env}) == 4)
    require(dict(value.split("=", 1) for value in env) == expected_env)
    safe = {
        "version": "2.0",
        "services": {
            "runner": {
                "image": IMAGE,
                "credentials": {
                    "host": "https://index.docker.io/v1/",
                    "username": "jobordu",
                    "password": "<hosted-reader>",
                },
                "env": [
                    "RUNNER_JIT_CONFIG=<memory-only>",
                    "EPHEMERAL=true",
                    "RUNNER_EXECUTION_USER=root",
                    "RUNNER_NAME=" + binding.runner_name,
                ],
            }
        },
        "profiles": {
            "compute": {
                "runner": {
                    "resources": {
                        "cpu": {"units": "4"},
                        "memory": {"size": "16Gi"},
                        "storage": [{"size": "96Gi"}],
                    }
                }
            },
            "placement": {
                binding.akash_group: {"pricing": {"runner": {"denom": "uact", "amount": 100000}}}
            },
        },
        "deployment": {"runner": {binding.akash_group: {"profile": "runner", "count": 1}}},
    }
    # Compare the complete raw shape after replacing only the two actual secrets.
    sanitized = yaml.safe_load(yaml.safe_dump(doc))
    sanitized["services"]["runner"]["credentials"]["password"] = "<hosted-reader>"  # noqa: S105 -- redaction marker
    sanitized["services"]["runner"]["env"] = safe["services"]["runner"]["env"]
    require(sanitized == safe)
    require(type(doc["deployment"]["runner"][binding.akash_group]["count"]) is int)
    require(
        type(doc["profiles"]["placement"][binding.akash_group]["pricing"]["runner"]["amount"])
        is int
    )
    return cast(dict[str, object], safe)


class ScannerLeaseProvenance:
    def __init__(
        self,
        *,
        binding: ScannerBinding,
        returned_runner_id: int,
        jit_ack_record: dict[str, object],
        jit_ack: DurableAck,
        encoded_jit_config: str,
        recorder: DurableRecorder,
        admission: Callable[[], None],
    ):
        self.binding = ScannerBinding(binding.to_dict())
        positive(returned_runner_id)
        row = validate_record(jit_ack_record)
        require(
            row["kind"] == "jit" and row["phase"] == "ACK" and row["binding"] == binding.to_dict()
        )
        validate_ack(row, jit_ack)
        require(row["details"]["runner_id"] == returned_runner_id)
        require(type(encoded_jit_config) is str and 0 < len(encoded_jit_config) <= 32768)
        require(callable(admission) and recorder is not None)
        self._config = encoded_jit_config
        self._receipt_secrets: tuple[bytes, ...] | None = ()
        self._admission = admission
        self._runner = returned_runner_id
        self._jit_sha = record_sha256(row)
        self.operation = ScannerOperation(binding, "lease", recorder)
        self.lease_operation = ScannerOperation(binding, "lease_bind", recorder)
        self._details: dict[str, object] | None = None
        self._lease_details: dict[str, object] | None = None
        self._deployment_ack: DurableAck | None = None
        self._lease_ack: DurableAck | None = None
        self._dseq: str | None = None
        self._content_sha: str | None = None
        self._client_bound = False
        self._create_started = False
        self._create_returned = False
        self._manifest: str | None = None
        self._lease_started = False

    @property
    def client_factory(self) -> Any:
        from .api import CIConsoleAPI

        return CIConsoleAPI

    def _admit(self) -> None:
        okay = False
        try:
            self._admission()
            okay = True
        except Exception:
            okay = False
        require(okay)

    def validate_call(self, **arguments: Any) -> None:
        require(
            arguments["gpu"] is False
            and arguments["image"] is None
            and arguments["env_vars"] is None
        )
        require(
            arguments["charter"] is None
            and arguments["deposit"] == 6
            and arguments["select"] == "cheapest"
        )
        require(
            arguments["preferred_providers"] is not None
            and set(arguments["preferred_providers"]) == PROVIDERS
        )
        require(arguments["backup_providers"] == [])
        require(type(arguments["receipt_path"]) is str and bool(arguments["receipt_path"]))
        require(arguments["receipt_operation_id"] == self.operation.operation_id)
        self._admit()

    def prepare(
        self, prepared: tuple[Path, PreparedDeploymentReceipt, bytes] | None, content: str
    ) -> None:
        require(self._details is None and prepared is not None)
        row = cast(tuple[Path, PreparedDeploymentReceipt, bytes], prepared)[1]
        require(row["state"] == "prepared" and row["operation_id"] == self.operation.operation_id)
        require(row["artifact_digest"] == hashlib.sha256(content.encode()).hexdigest())
        require(row["group_population"] == [{"gseq": 1, "name": self.binding.akash_group}])
        public = public_sdl_request(content, self.binding, self._config)
        # The complete SDL was validated above. Keep the reader and JIT material
        # only in memory so a credential-shaped transaction echo is also rejected.
        password = yaml.safe_load(content)["services"]["runner"]["credentials"]["password"]
        self._receipt_secrets = _receipt_secret_views(self._config, password)
        self._content_sha = hashlib.sha256(content.encode()).hexdigest()
        self._details = {
            "owner": row["expected_owner"],
            "group_population": row["group_population"],
            "public_request_sha256": record_sha256(public),
            "wire_request_sha256": self._content_sha,
            "returned_runner_id": self._runner,
            "jit_ack_record_sha256": self._jit_sha,
            "deposit_usd": "6",
        }
        self._admit()
        self.operation.no_wire(self._details)

    def bound_client(self, client: Any) -> Any:
        require(self._details is not None and not self._client_bound)
        key = getattr(client, "api_key", None)
        if self._receipt_secrets is not None and type(key) is str and key:
            self._receipt_secrets += (key.encode(),)
        self._client_bound = True
        return _ScannerClient(client, self)

    def before_create(self, content: str, deposit: float) -> None:
        require(self._details is not None and not self._create_started)
        require(hashlib.sha256(content.encode()).hexdigest() == self._content_sha and deposit == 6)
        self._admit()
        self._create_started = True
        self.operation.before_wire(cast(dict[str, object], self._details))
        self._admit()

    def created(self, dseq: str) -> None:
        require(
            self._create_started
            and self._create_returned
            and self._dseq is None
            and self._details is not None
        )
        details = dict(cast(dict[str, object], self._details), dseq=dseq)
        self._deployment_ack = self.operation.ack(details)
        self._dseq = dseq

    def local_create_response(self, dseq: object, response: dict[str, Any]) -> dict[str, str]:
        """Safe view for the local receipt only; retain the full caller response."""
        require(self._create_returned)
        require(type(dseq) in (str, int) and re.fullmatch(r"[1-9][0-9]{0,19}", str(dseq)))
        require(int(str(dseq)) <= 2**64 - 1)
        result = {"dseq": str(dseq)}
        if self._receipt_secrets is None:
            return result
        require(bool(self._receipt_secrets))
        for key in (
            "txhash",
            "txHash",
            "tx_hash",
            "transactionHash",
            "transaction_hash",
            "transactionId",
            "transaction_id",
        ):
            value = response.get(key)
            if type(value) is not str or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
                continue
            candidate = value.lower().encode("ascii")
            if any(
                secret and (candidate in secret.lower() or secret.lower() in candidate)
                for secret in self._receipt_secrets
            ):
                continue
            result[key] = value
        return result

    def before_lease(
        self, *, dseq: str, provider: str, manifest: str, gseq: int, oseq: int
    ) -> None:
        require(
            self._deployment_ack is not None and not self._lease_started and dseq == self._dseq
        )
        require(
            provider in PROVIDERS
            and type(gseq) is int
            and gseq == 1
            and type(oseq) is int
            and oseq == 1
        )
        require(type(manifest) is str and bool(manifest) and manifest == self._manifest)
        self._admit()
        self._lease_started = True
        details = {
            "owner": cast(dict, self._details)["owner"],
            "dseq": dseq,
            "provider": provider,
            "gseq": gseq,
            "returned_runner_id": self._runner,
            "jit_ack_record_sha256": self._jit_sha,
            "deployment_ack_record_sha256": record_sha256(self.operation.public_record),
            "public_request_sha256": cast(dict, self._details)["public_request_sha256"],
            "wire_request_sha256": cast(dict, self._details)["wire_request_sha256"],
        }
        self._lease_details = details
        self.lease_operation.no_wire(details)
        self._admit()
        self.lease_operation.before_wire(details)
        self._admit()

    def leased(self) -> None:
        require(
            self._lease_started and self._lease_details is not None and self._lease_ack is None
        )
        self._lease_ack = self.lease_operation.ack(
            dict(cast(dict, self._lease_details), lease_response_received=True)
        )

    def hold_unknown(self) -> NoReturn:
        raise ScannerProvenanceHeld(
            "Scanner-v2 outcome held; no retry, replacement or implicit close permitted"
        )

    def public_result(self, result: dict) -> dict[str, object]:
        require(
            self._deployment_ack is not None
            and self._lease_ack is not None
            and self._lease_details is not None
        )
        require(
            str(result.get("dseq")) == self._dseq
            and result.get("provider") == cast(dict, self._lease_details)["provider"]
        )
        return {
            "schema": "scanner-v2-deploy-result/v1",
            "dseq": self._dseq,
            "owner": cast(dict, self._details)["owner"],
            "provider": cast(dict, self._lease_details)["provider"],
            "gseq": 1,
            "returned_runner_id": self._runner,
            "deployment_ack_record": self.operation.public_record,
            "deployment_ack": self._deployment_ack,
            "lease_ack_record": self.lease_operation.public_record,
            "lease_ack": self._lease_ack,
            "cleanup_qualified": False,
        }


class _ScannerClient:
    """Restrict only the optional v2 branch; old callers keep the original client."""

    def __init__(self, client: Any, provenance: ScannerLeaseProvenance):
        self._client, self._provenance = client, provenance

    def __getattr__(self, name: str) -> Any:
        # Permit ordinary observations/properties, never an unjournalled effect.
        value = getattr(self._client, name)
        require(not callable(value) or name.startswith("get_") or name == "account_address")
        return value

    def create_deployment(self, sdl_content: str, deposit: float = 5.0) -> dict:
        require(self._client.account_address() == cast(dict, self._provenance._details)["owner"])
        self._provenance.before_create(sdl_content, deposit)
        result = None
        try:
            result = self._client.create_deployment(sdl_content, deposit=deposit)
        except Exception:
            result = None
        require(type(result) is dict)
        self._provenance._create_returned = True
        self._provenance._manifest = cast(dict, result).get("manifest")
        return cast(dict, result)

    def create_lease(
        self, dseq: str, provider: str, manifest: str, gseq: int = 1, oseq: int = 1
    ) -> dict:
        require(self._client.account_address() == cast(dict, self._provenance._details)["owner"])
        self._provenance.before_lease(
            dseq=dseq, provider=provider, manifest=manifest, gseq=gseq, oseq=oseq
        )
        result = None
        try:
            result = self._client.create_lease(
                dseq=dseq, provider=provider, manifest=manifest, gseq=gseq, oseq=oseq
            )
        except Exception:
            result = None
        require(type(result) is dict and bool(result))
        self._provenance.leased()
        return cast(dict, result)

    def close_deployment(self, dseq: str) -> NoReturn:
        self._provenance.hold_unknown()


def deploy_scanner_v2(
    *,
    sdl_path: str,
    binding: ScannerBinding,
    returned_runner_id: int,
    jit_ack_record: dict[str, object],
    jit_ack: DurableAck,
    encoded_jit_config: str,
    recorder: DurableRecorder,
    receipt_path: str,
    admission: Callable[[], None],
) -> dict[str, object]:
    """Invoke the real installed SDK deploy path, never an injected fake deploy callable."""
    from .deploy import deploy

    provenance = ScannerLeaseProvenance(
        binding=binding,
        returned_runner_id=returned_runner_id,
        jit_ack_record=jit_ack_record,
        jit_ack=jit_ack,
        encoded_jit_config=encoded_jit_config,
        recorder=recorder,
        admission=admission,
    )
    result = deploy(
        sdl_path,
        preferred_providers=sorted(PROVIDERS),
        backup_providers=[],
        deposit=6,
        select="cheapest",
        quiet_wallet=True,
        receipt_path=receipt_path,
        receipt_operation_id=provenance.operation.operation_id,
        scanner_provenance=provenance,
    )
    return provenance.public_result(result)
