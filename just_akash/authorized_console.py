"""One redeemed CI create, checked at the actual Console transport boundary.

Inputs come from a trusted in-process broker, never decoded HTTP authority.
The broker persists SUBMITTING before handing over authorization; uncertain
outcomes retain that operation for reconciliation, not another POST.
"""

from __future__ import annotations

import base64
import json
import math
import re
import threading
import time
import urllib.request
from typing import Any

from akash_lease_core import (
    CREATE_PERMIT_AUDIENCE,
    AdmissionRequest,
    BackendIdentity,
    BackendKind,
    CreatePermit,
    CreateSubmissionAuthorization,
    OwnerBudgetScope,
    canonical_journal_digest,
    canonical_payload_digest,
    is_canonical_akash_owner,
)

from .api import CI_CONSOLE_ORIGIN, CIConsoleAPI
from .deployment_receipt import artifact_identity

CONSOLE_BACKEND = BackendIdentity(BackendKind.MEDIATED, CI_CONSOLE_ORIGIN)
_OWNER_PROBE_URL = CI_CONSOLE_ORIGIN + "/v1/create-jwt-token"
_OWNER_PROBE_BODY = json.dumps(
    {"data": {"ttl": 30, "leases": {"access": "scoped", "scope": ["status"]}}}
).encode("utf-8")


class CreateHeld(RuntimeError):
    """No new deployment POST was sent for this attempt; prior creates may exist."""


class CreateUnknown(RuntimeError):
    """The POST may have committed; reconcile the retained exact operation."""


def create_body(sdl_content: str, deposit: float) -> bytes:
    """Exact compatibility payload bytes, not a deployment spending ceiling.

    Console manages deployment funding from account credits. Its legacy
    deposit field does not establish the broker's financial exposure bound.
    """
    if (
        not isinstance(sdl_content, str)
        or not sdl_content
        or type(deposit) not in (int, float)
        or not math.isfinite(deposit)
        or deposit < 0.5
    ):
        raise CreateHeld("Invalid Console create payload")
    return json.dumps({"data": {"sdl": sdl_content, "deposit": deposit}}).encode("utf-8")


def create_runtime_body(sdl_content: str, runtime_limit_hours: float) -> bytes:
    """Preserve the runtime-limit payload; it proves no financial exposure bound.

    The trusted broker must derive exposure separately for these exact bytes and
    the backend policy. No deposit is inferred or added to this payload.
    """
    if (
        not isinstance(sdl_content, str)
        or not sdl_content
        or type(runtime_limit_hours) not in (int, float)
        or runtime_limit_hours <= 0
        or (type(runtime_limit_hours) is float and not math.isfinite(runtime_limit_hours))
    ):
        raise CreateHeld("Invalid Console runtime-limit create payload")
    try:
        return json.dumps(
            {"data": {"sdl": sdl_content, "runtimeLimitHours": runtime_limit_hours}},
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, OverflowError):
        raise CreateHeld("Invalid Console runtime-limit create payload") from None


class AuthorizedConsoleCreate(CIConsoleAPI):
    """Single-use transport configured by the trusted controller.

    An account may serve other purposes. Ownership is checked for this exact
    request; it does not grant cleanup authority over other wallet resources.
    """

    def __init__(self, api_key: str, *, owner: str, policy_revision: str, broker: str):
        if (
            not is_canonical_akash_owner(owner)
            or not isinstance(policy_revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", policy_revision) is None
            or not isinstance(broker, str)
            or not broker
        ):
            raise CreateHeld("Invalid trusted Console create policy")
        super().__init__(api_key)
        self._owner, self._policy_revision, self._broker = owner, policy_revision, broker
        self._submit_lock = threading.Lock()
        self._used = False
        self._dispatched = False
        self._binding: (
            tuple[AdmissionRequest, CreatePermit, CreateSubmissionAuthorization] | None
        ) = None

    def create_deployment(self, sdl_content: str, deposit: float = 5.0) -> dict[str, Any]:
        raise CreateHeld("Use submit with a durably redeemed create authorization")

    def account_address(self) -> str:
        """Read the issuer from this fixed HTTPS mediator's status-only token.

        The token remains local and is not a signature or chain binding proof;
        the trusted broker independently establishes its signer policy.
        """
        token = self.create_jwt("0", ttl=30, scope=["status"])
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("invalid token structure")
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            owner = claims.get("iss")
            if not is_canonical_akash_owner(owner):
                raise ValueError("invalid owner")
            return owner
        except Exception:
            raise CreateHeld("Console credential owner could not be established") from None

    def submit(
        self,
        *,
        request: AdmissionRequest,
        permit: CreatePermit,
        authorization: CreateSubmissionAuthorization,
        sdl_content: str,
        deposit: float,
    ) -> dict[str, Any]:
        return self._submit(
            request=request,
            permit=permit,
            authorization=authorization,
            body=create_body(sdl_content, deposit),
        )

    def submit_runtime_limit(
        self,
        *,
        request: AdmissionRequest,
        permit: CreatePermit,
        authorization: CreateSubmissionAuthorization,
        sdl_content: str,
        runtime_limit_hours: float,
    ) -> dict[str, Any]:
        """Submit one redeemed create with the exact runtime-limit wire variant."""
        return self._submit(
            request=request,
            permit=permit,
            authorization=authorization,
            body=create_runtime_body(sdl_content, runtime_limit_hours),
        )

    def _submit(
        self,
        *,
        request: AdmissionRequest,
        permit: CreatePermit,
        authorization: CreateSubmissionAuthorization,
        body: bytes,
    ) -> dict[str, Any]:
        if not self._submit_lock.acquire(blocking=False):
            raise CreateHeld("Console create submission is already in progress")
        try:
            return self._submit_locked(
                request=request,
                permit=permit,
                authorization=authorization,
                body=body,
            )
        finally:
            self._submit_lock.release()

    def _submit_locked(
        self,
        *,
        request: AdmissionRequest,
        permit: CreatePermit,
        authorization: CreateSubmissionAuthorization,
        body: bytes,
    ) -> dict[str, Any]:
        if self._used:
            raise CreateHeld("This Console create client is already consumed")
        if not all(
            isinstance(value, expected)
            for value, expected in (
                (request, AdmissionRequest),
                (permit, CreatePermit),
                (authorization, CreateSubmissionAuthorization),
            )
        ):
            raise CreateHeld("Trusted in-process create authority is required")
        self._binding = request, permit, authorization
        try:
            self._validate(body)
            # A fresh authenticated account read uses this same fixed-origin key.
            if self.account_address() != self._owner:
                raise CreateHeld("Console credential owner differs from the prepared owner")
            envelope = super()._request("POST", "/v1/deployments", json.loads(body))
            data = envelope.get("data", envelope) if isinstance(envelope, dict) else {}
            response = data if isinstance(data, dict) else envelope
            dseq = response.get("dseq")
            if (
                type(dseq) not in (str, int)
                or re.fullmatch(r"[1-9][0-9]*", str(dseq)) is None
                or int(str(dseq)) >= 2**64
            ):
                raise CreateUnknown("Console create response needs exact reconciliation")
            return response
        except Exception as error:
            if self._dispatched:
                raise CreateUnknown(
                    "Console create outcome requires exact reconciliation"
                ) from None
            if isinstance(error, CreateHeld):
                raise
            raise CreateHeld("Console create preconditions could not be established") from None
        finally:
            self._binding = None

    def _validate(self, body: bytes) -> None:
        if self._binding is None:
            raise CreateHeld("No bound Console create authority")
        request, permit, authorization = self._binding
        prepared = request.prepared
        now = int(time.time())
        payload_sdl = json.loads(body)["data"]["sdl"]
        population, _, _ = artifact_identity(payload_sdl)
        if (
            prepared.owner_candidate.backend != CONSOLE_BACKEND
            or prepared.owner_candidate.owner != self._owner
            or request.scope.owner != self._owner
            or request.exposure.backend_policy_revision != self._policy_revision
            or prepared.request_digest != canonical_payload_digest(body)
            or prepared.sdl_digest != canonical_payload_digest(payload_sdl.encode("utf-8"))
            or population
            != [{"gseq": group.gseq, "name": group.group_name} for group in prepared.groups]
            or permit.prepared_operation_digest != canonical_journal_digest(prepared)
            or authorization.operation_id != prepared.operation_id
            or authorization.permit_digest != canonical_journal_digest(permit)
            or authorization.broker_identity != self._broker
            or authorization.presenter_identity != prepared.producer.subject
            or authorization.audience != CREATE_PERMIT_AUDIENCE
            or authorization.policy_bindings != permit.policy_bindings
            or {item.scope for item in authorization.policy_bindings}
            != {OwnerBudgetScope(request.scope.chain_id, self._owner), request.scope}
            or not authorization.issued_at <= now < authorization.valid_until
            or now > authorization.issued_at + 5
            or not request.exposure.observed_at <= now < request.exposure.valid_until
        ):
            raise CreateHeld("Console create authority differs from the exact current request")

    def _open_request(self, request: urllib.request.Request) -> Any:
        if not request.full_url.startswith(CI_CONSOLE_ORIGIN + "/"):
            raise CreateHeld("Console request differs from the registered HTTPS origin")
        if request.get_method() == "POST" and request.full_url == _OWNER_PROBE_URL:
            if self._binding is None or self._used or request.data != _OWNER_PROBE_BODY:
                raise CreateHeld("Only the bound status-only owner probe is supported")
            return super()._open_request(request)
        if request.get_method() not in ("GET", "POST") or (
            request.get_method() == "POST"
            and request.full_url != CI_CONSOLE_ORIGIN + "/v1/deployments"
        ):
            raise CreateHeld("Only the exact deployment create mutation is supported")
        if (
            request.get_method() == "POST"
            and request.full_url == CI_CONSOLE_ORIGIN + "/v1/deployments"
        ):
            if self._used:
                raise CreateHeld("Console create POST already consumed")
            if not isinstance(request.data, bytes):
                raise CreateHeld("Console create requires exact prepared bytes")
            self._validate(request.data)
            # Mark before invoking the real opener. A timeout, redirect or bad
            # response must never restore this slot or license a second create.
            self._used = self._dispatched = True
        return super()._open_request(request)
