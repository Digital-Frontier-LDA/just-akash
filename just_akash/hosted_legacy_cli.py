"""Code-owned HOSTED CLI join, disabled until a reviewed bootstrap registers it.

Candidates/constructors are not principals or capacity permits. Server acquisition,
shared CAS, one-use consume and native cleanup remain mandatory. No environment,
plugin path, caller token or HTTP mode can install this registration.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import math
import re
import sys
import time
import types
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from . import guardian_legacy_lease_backend
    from .api import AkashConsoleAPI

    class _InspectedFunction(Protocol):
        __code__: types.CodeType
        __module__: str
        __wrapped__: types.FunctionType
        __closure__: tuple[types.CellType, ...] | None


CLI_SHA256 = "1993415eff04f02454422ae1321d97342eb2c472e19868a975b9f1eada9f8ac7"
DEPLOY_SHA256 = "a5f6bcffc47cc98ee1f24d70763a76a208110e61ce0812b099877de1893caa4f"
PROFILE_SHA256 = "ee6c583e379a2e81ff9f020dcab5777f95f8309938f29d6554acd3ad72958d35"
BACKEND_SHA256 = "7940aa272efd62e469a9404aff8b00a0b1ce36ec14198f67cd20299b682107b2"
CLEANUP_SHA256 = "9c77e4a112d061e701a0a58332a8e0ff1dead63ad69928116ef327781e986aa8"
WALLET_SHA256 = "9e636ec5107d13d17f52fabfda08b06a66c33204f245ed07d9a22f6bc82bf2bf"

CONFIDENTIAL_SHA256 = "22cc4468e2219ebe525b6e4aeb655bf6966c49e5427b751a611f139d3848a898"


class HostedCLIUnknown(Exception):
    """Finite nonretry indication, never raw credential/transport error text."""


def require(value):
    if not value:
        raise HostedCLIUnknown("hosted-legacy-cli-UNKNOWN-no-replay")


@dataclass(frozen=True, repr=False)
class HostedGHAPrincipals:
    """Private short-lived raw GHA inputs, NOT verified execution/authority."""

    oidc: str = field(repr=False)
    actions_read_token: str = field(repr=False)
    valid_until: int


@dataclass(frozen=True, repr=False)
class ProvisionCLIRegistration:
    sdk_parent_ref: str
    receipt_policy: object = field(repr=False)
    operation_id: str
    receipt_path: str
    source_revision: str
    slot: str
    original_end: float
    original_deadline_at: int
    chain_id: str
    key_candidate: str
    acquire_principals: object = field(repr=False)


@dataclass(frozen=True, repr=False)
class CleanupCLIRegistration:
    receipt_policy: object = field(repr=False)
    consume_wire: bytes = field(repr=False)
    original_end: float
    cleanup_deadline_at: int
    acquire_principals: object = field(repr=False)


@dataclass(repr=False)
class HostedCallResult:
    """Observation only. None is unmeasured; () never proves global zero debt."""

    command: str
    entered: bool = False
    consume_locators: tuple[bytes, ...] | None = field(default=None, repr=False)
    cleanup_observation: object = field(default=None, repr=False)
    locator_custody_unknown: bool = False
    primary_failure_class: str | None = None


def _reviewed_hosted_cli() -> InstalledHostedCLI | None:
    # A concrete source/ReceiptPolicy/hosted token acquisition/cross-job custody
    # registration is still missing. Never infer one from env or a constructor.
    return None


class InstalledHostedCLI:
    def __init__(self, registration):
        require(type(registration) in (ProvisionCLIRegistration, CleanupCLIRegistration))
        self.registration = registration
        self.result: HostedCallResult | None = None
        self.used = False
        self._old: Callable[[], InstalledHostedCLI | None] | None = None
        self._resolver: Callable[[], InstalledHostedCLI | None] | None = None
        self._capture: tuple[AkashConsoleAPI, str, str, str, str] | None = None

    def __enter__(self):
        global _reviewed_hosted_cli
        require(self._resolver is None and _reviewed_hosted_cli() is None)
        self._old = _reviewed_hosted_cli
        self._resolver = lambda: self
        _reviewed_hosted_cli = self._resolver
        return self

    def __exit__(self, kind, error, tb):
        global _reviewed_hosted_cli
        self._capture = None  # raw transformed SDL/manifest never outlive this hosted call
        if _reviewed_hosted_cli is self._resolver:
            _reviewed_hosted_cli = cast("Callable[[], InstalledHostedCLI | None]", self._old)
        elif error is None:
            raise HostedCLIUnknown("hosted-cli-registration-retirement-UNKNOWN")
        return False


def install_root_hosted_cli(registration):
    """Explicit source-owned bootstrap only; no ambient activation or authority."""
    return InstalledHostedCLI(registration)


def _lifetime(end, until):
    require(type(end) in (int, float) and math.isfinite(end))
    require(type(until) is int and until > 0)
    require(time.monotonic() < end and time.time() < until)


def _sources(backend, end, until):
    _lifetime(end, until)
    base = Path(__file__).parent
    for name, sha in (
        ("cli.py", CLI_SHA256),
        ("deploy.py", DEPLOY_SHA256),
        ("request_profile.py", PROFILE_SHA256),
        ("guardian_legacy_lease_backend.py", BACKEND_SHA256),
        ("legacy_capacity_cleanup.py", CLEANUP_SHA256),
        ("wallet_pool.py", WALLET_SHA256),
        ("api.py", backend.API_SHA256),
        ("atomic_lease_admission.py", backend.HOOK_SHA256),
    ):
        raw = backend._read_source(base / name, sha)
        if name in {"cli.py", "deploy.py"}:
            module = importlib.import_module(cast(str, __package__) + "." + name[:-3])
            require(module is not None and Path(cast(str, module.__file__)) == base / name)
            compiled = compile(ast.parse(raw), str(base / name), "exec", dont_inherit=True)
            function_name = "main" if name == "cli.py" else "deploy"
            actual = getattr(module, function_name, None)
            expected = next(
                c
                for c in compiled.co_consts
                if isinstance(c, types.CodeType) and c.co_name == function_name
            )
            require(type(actual) is types.FunctionType and actual.__module__ == module.__name__)
            actual = cast("_InspectedFunction", actual)
            if name == "deploy.py":
                confidential_path = base / "_confidential.py"
                confidential_raw = backend._read_source(confidential_path, CONFIDENTIAL_SHA256)
                confidential_code = compile(
                    ast.parse(confidential_raw), str(confidential_path), "exec", dont_inherit=True
                )
                decorator = next(
                    c
                    for c in confidential_code.co_consts
                    if isinstance(c, types.CodeType) and c.co_name == "sdl_operation"
                )
                wrapper = next(
                    c
                    for c in decorator.co_consts
                    if isinstance(c, types.CodeType) and c.co_name == "scoped"
                )
                require(actual.__code__.co_filename == str(confidential_path))
                require(backend._code_shape(actual.__code__) == backend._code_shape(wrapper))
                wrapped = actual.__wrapped__
                closure = dict(
                    zip(
                        actual.__code__.co_freevars,
                        (
                            cell.cell_contents
                            for cell in cast("tuple[types.CellType, ...]", actual.__closure__)
                        ),
                        strict=True,
                    )
                )
                require(closure["function"] is wrapped)
                actual = wrapped
            require(type(actual) is types.FunctionType)
            require(actual.__code__.co_filename == str(base / name))
            require(backend._code_shape(actual.__code__) == backend._code_shape(expected))
        _lifetime(end, until)  # includes every held FD's retirement


def capture_prepared_lease(client, prepared_receipt, dseq, sdl, manifest):
    """Private actual deploy producer; raw values stay only in hosted memory.

    No registration means no capture. A new DSEQ is deliberately unsupported,
    never an implicit transfer of a retained parent or UNKNOWN capacity debt.
    """
    installed = _reviewed_hosted_cli()
    if installed is None:
        return
    require(type(installed) is InstalledHostedCLI and installed.used)
    reg = installed.registration
    require(type(reg) is ProvisionCLIRegistration)
    require(installed.result is not None and cast(HostedCallResult, installed.result).entered)
    from . import deployment_receipt
    from . import guardian_legacy_lease_backend as backend

    _sources(backend, reg.original_end, reg.original_deadline_at)
    require(type(client) is sys.modules[cast(str, __package__) + ".api"].AkashConsoleAPI)
    require(installed._capture is None or installed._capture[0] is client)
    require(type(sdl) is str and 0 < len(sdl.encode("utf-8")) <= 2 * 1024 * 1024)
    require(type(manifest) is str and 0 < len(manifest.encode("utf-8")) <= 2 * 1024 * 1024)
    require(type(dseq) in (str, int) and type(dseq) is not bool)
    require(re.fullmatch(r"[1-9][0-9]*", str(dseq)) is not None and int(dseq) < 2**64)
    require(installed._capture is None or installed._capture[1:4] == (str(dseq), sdl, manifest))
    require(type(prepared_receipt) is tuple and len(prepared_receipt) == 3)
    path, prepared, encoded = prepared_receipt
    require(type(prepared) is dict and type(encoded) is bytes)
    require(str(path) == reg.receipt_path)
    require(deployment_receipt._canonical_bytes(prepared) == encoded)
    require(prepared.get("operation_id") == reg.operation_id)
    owner = client.account_address()
    population, population_digest, artifact_digest = deployment_receipt.artifact_identity(sdl)
    require(
        prepared.get("state") == "submitting"
        and prepared.get("operation_id") == reg.operation_id
        and prepared.get("expected_owner") == owner
        and prepared.get("group_population") == population
        and prepared.get("group_population_digest") == population_digest
        and prepared.get("artifact_digest") == artifact_digest
    )
    value = (client, str(dseq), sdl, manifest, owner)
    require(installed._capture is None or installed._capture == value)
    installed._capture = value
    _lifetime(reg.original_end, reg.original_deadline_at)


def _principals(registration, end, until):
    _lifetime(end, until)
    require(callable(registration.acquire_principals))
    raw = registration.acquire_principals()
    require(type(raw) is HostedGHAPrincipals)
    require(
        all(
            type(x) is str
            and 0 < len(x) <= 16384
            and all(ord(c) >= 32 and ord(c) != 127 for c in x)
            for x in (raw.oidc, raw.actions_read_token)
        )
    )
    require(type(raw.valid_until) is int and time.time() < raw.valid_until)
    _lifetime(end, until)
    return raw


@contextmanager
def _registered_cli_context(installed, command, args):
    require(type(installed) is InstalledHostedCLI and not installed.used)
    require(command in {"deploy", "destroy"} and args.command == command)
    installed.used = True  # one original CLI call; never reuse after any failure
    reg = installed.registration
    require(
        type(reg) is (ProvisionCLIRegistration if command == "deploy" else CleanupCLIRegistration)
    )
    from . import guardian_legacy_lease_backend as backend
    from . import legacy_capacity_cleanup as cleanup

    until = reg.original_deadline_at if command == "deploy" else reg.cleanup_deadline_at
    end = reg.original_end
    _lifetime(end, until)
    require(type(reg.receipt_policy) is backend.ReceiptPolicy)
    _sources(backend, end, until)
    _principals(reg, end, until)  # missing acquisition refuses BEFORE dispatch/effect
    require(_reviewed_hosted_cli() is installed)
    result = HostedCallResult(command)
    installed.result = result

    def credentials(client):
        require(_reviewed_hosted_cli() is installed)
        _sources(backend, end, until)
        raw = _principals(reg, end, until)
        # Actual backend/cleanup rejoin validates the selected original client,
        # its private key/header equality and lower API/source methods.
        value = backend.PrincipalCredentials(raw.oidc, raw.actions_read_token, client.api_key)
        _lifetime(end, until)
        return value

    if command == "deploy":
        require(reg.sdk_parent_ref in backend.SDK_PARENTS)
        require(
            type(reg.operation_id) is str
            and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", reg.operation_id) is not None
        )
        require(
            type(reg.source_revision) is str
            and re.fullmatch(r"[0-9a-f]{40}", reg.source_revision) is not None
        )
        require(type(reg.slot) is str and 0 < len(reg.slot) <= 128)
        require(
            type(reg.receipt_path) is str
            and reg.receipt_path
            and args.receipt_path == reg.receipt_path
            and args.receipt_operation_id == reg.operation_id
        )
        require(type(reg.chain_id) is str and reg.chain_id == "akashnet-2")
        require(type(reg.key_candidate) is str and 0 < len(reg.key_candidate) <= 256)

        def context(send):
            require(_reviewed_hosted_cli() is installed)
            _sources(backend, end, until)
            require(
                cast(HostedCallResult, installed.result).entered and installed._capture is not None
            )
            client, dseq, sdl, manifest, owner = cast(
                "tuple[AkashConsoleAPI, str, str, str, str]", installed._capture
            )
            require(
                cast(
                    "guardian_legacy_lease_backend.GuardianLeaseBackend", registration.backend
                )._client
                is client
            )
            require(type(send) is backend.hook.LeaseIdentity and send.dseq == dseq)
            require(hashlib.sha256(manifest.encode("utf-8")).hexdigest() == send.manifest_sha256)
            require(client.account_address() == owner)
            body = json.dumps(
                {
                    "manifest": manifest,
                    "leases": [
                        {
                            "dseq": send.dseq,
                            "gseq": send.gseq,
                            "oseq": send.oseq,
                            "provider": send.provider,
                        }
                    ],
                }
            ).encode("utf-8")
            value = backend.PreparedLeaseContext(
                reg.operation_id,
                reg.chain_id,
                owner,
                reg.key_candidate,
                reg.slot,
                reg.source_revision,
                until,
                sdl,
                body,
            )
            require(type(value) is backend.PreparedLeaseContext)
            require(
                value.operation_id == reg.operation_id
                and value.source_revision == reg.source_revision
                and value.slot == reg.slot
                and value.original_deadline_at == until
            )
            _lifetime(end, until)
            return value

        manager = backend.install_root_broker_registration(
            client=None,
            sdk_parent_ref=reg.sdk_parent_ref,
            policy=reg.receipt_policy,
            context_supplier=context,
            credentials_supplier=credentials,
            end=end,
            original_deadline_at=until,
            clock=time.monotonic,
            utc_clock=time.time,
        )
        with manager as registration:
            result.entered = True
            try:
                yield result
            except BaseException as error:
                result.primary_failure_class = type(error).__name__
                raise
            finally:
                try:
                    result.consume_locators = cast(
                        "guardian_legacy_lease_backend.GuardianLeaseBackend", registration.backend
                    ).retained_cleanup_locators()
                except BaseException:
                    result.locator_custody_unknown = True
                    # A lost reserve/consume ACK never becomes a retryable CLI1
                    # or fabricated empty debt. Preserve only its finite primary
                    # class in the result, then enforce the no-replay CLI78.
                    raise HostedCLIUnknown("hosted-cli-locator-custody-UNKNOWN") from None
    else:
        require(type(reg.consume_wire) is bytes)
        historical = cleanup.historical_consume(reg.consume_wire, reg.receipt_policy)
        require(historical["observed_at"] <= time.time())
        require(
            args.dseq == historical["identity"]["dseq"]
            and args.expected_owner == historical["identity"]["owner"]
        )
        manager = cleanup.install_root_cleanup(
            client=None,
            consume_wire=reg.consume_wire,
            policy=reg.receipt_policy,
            credentials_supplier=credentials,
            end=end,
            cleanup_deadline_at=until,
            clock=time.monotonic,
            utc_clock=time.time,
        )
        with manager as callback:
            result.entered = True
            yield result
            result.cleanup_observation = callback.observation
    _sources(backend, end, until)
    require(_reviewed_hosted_cli() is installed)


@contextmanager
def hosted_cli_context(command, args):
    try:
        installed = _reviewed_hosted_cli()
    except BaseException:
        raise HostedCLIUnknown("hosted-cli-registration-UNKNOWN") from None
    if installed is None:
        yield None  # Entire original off-mode dispatch/error behavior.
        return
    primary = False
    try:
        with _registered_cli_context(installed, command, args) as result:
            try:
                yield result
            except BaseException:
                primary = True
                raise
    except BaseException:
        if primary:
            raise  # Never mask the original API/UNKNOWN/cancellation failure.
        # Includes lifetime/source/client/FD/registration retirement after a
        # successful-looking DELETE. Workflow must not replay that operation.
        raise HostedCLIUnknown("hosted-legacy-cli-UNKNOWN-no-replay") from None
