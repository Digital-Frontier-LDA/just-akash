"""Explicit hosted entry: same-process registration and local custody callback.

Run with ``python -m just_akash.hosted_cli_bootstrap deploy|destroy ...`` only
under a reviewed source configuration. Default-off refuses before legacy CLI.
The callback has no storage implementation here and is NOT a durable ACK or a
permit. Inner backend retirement precedes capture; outer CLI retirement follows.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from . import hosted_legacy_cli as hosted


@dataclass(frozen=True)
class CLICompletion:
    """Finite primary observation only; no raw exception, credential or payload."""

    kind: Literal["return", "system-exit", "cancel", "exception"]
    exit_status: int | None
    locator_unknown: bool


@dataclass(frozen=True, repr=False)
class HostedCLIBootstrap:
    """Source configuration, not caller-presented registration/authority."""

    registration: hosted.ProvisionCLIRegistration | hosted.CleanupCLIRegistration
    capture: Callable[[hosted.HostedCallResult | None, CLICompletion, float], None] = field(
        repr=False
    )

    def __post_init__(self) -> None:
        if type(self.registration) not in (
            hosted.ProvisionCLIRegistration,
            hosted.CleanupCLIRegistration,
        ) or not callable(self.capture):
            raise hosted.HostedCLIUnknown("hosted-bootstrap-unavailable-no-replay")


def _reviewed_hosted_cli_bootstrap() -> HostedCLIBootstrap | None:
    # Root must supply the exact approved registration and bounded source-owned
    # callback. Never infer either from flags, env, metadata or a plugin path.
    return None


def _success(primary: BaseException | None) -> bool:
    return primary is None or (
        type(primary) is SystemExit and (primary.code is None or primary.code == 0)
    )


def _first_cancellation(
    primary: BaseException | None, error: BaseException
) -> BaseException | None:
    if _success(primary) and isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
        return error
    return primary


def _completion(
    primary: BaseException | None, result: hosted.HostedCallResult | None
) -> CLICompletion:
    cleanup_unknown = False
    if result is not None and result.command == "destroy":
        from .legacy_capacity_cleanup import ClosedCapacityObservation

        observation = result.cleanup_observation
        cleanup_unknown = not (
            type(observation) is ClosedCapacityObservation
            and observation.execution_closed is True
            and observation.settlement_proven is False
            and observation.lease_permit is False
        )  # Local completion shape only; never a new permit or signature proof.
    unknown = (
        result is None
        or not result.entered
        or result.locator_custody_unknown
        or (result.command == "deploy" and result.consume_locators is None)
        or cleanup_unknown
    )
    if primary is None:
        return CLICompletion("return", 0, unknown)
    if type(primary) is SystemExit:
        code = primary.code
        status = 0 if code is None else code if type(code) is int else None
        return CLICompletion("system-exit", status, unknown)
    return CLICompletion(
        "cancel"
        if isinstance(primary, (asyncio.CancelledError, KeyboardInterrupt))
        else "exception",
        None,
        unknown,
    )


def main() -> int:
    """One CLI call, one local capture, no retries or deadline replenishment.

    Capture is attempted once after CLI completion while the outer registration
    remains installed, even on a primary failure. It receives the SAME original
    end and must itself be bounded. No callback/durability/hard-stop proof is
    supplied by this wrapper. Failure cannot turn an otherwise successful call
    into success; original nonzero exits/exceptions/cancellation are preserved.
    """
    try:
        configured = _reviewed_hosted_cli_bootstrap()
        if type(configured) is not HostedCLIBootstrap:
            return 78
        configured.__post_init__()
        registration = configured.registration
        command = "deploy" if type(registration) is hosted.ProvisionCLIRegistration else "destroy"
        if len(sys.argv) < 2 or sys.argv[1] != command:
            return 78
        until = (
            registration.original_deadline_at
            if type(registration) is hosted.ProvisionCLIRegistration
            else cast(hosted.CleanupCLIRegistration, registration).cleanup_deadline_at
        )
        hosted._lifetime(registration.original_end, until)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except BaseException:
        return 78  # No CLI entry or silent unmetered fallback.

    primary: BaseException | None = None
    uncertain = False
    before = hosted._reviewed_hosted_cli
    manager: hosted.InstalledHostedCLI | None = None
    try:
        manager = hosted.install_root_hosted_cli(registration)
        with manager as active:
            try:
                from . import cli

                returned = cli.main()
                if returned is not None:
                    primary = SystemExit(returned)  # Normal console-entry status semantics.
            except BaseException as error:
                primary = error  # Private only; never format or serialize it.
            result = active.result
            observed = _completion(primary, result)
            try:
                # Exact raw result: None/UNKNOWN is never replaced with ()/[] or
                # a newly fabricated receipt. Callback owns any storage protocol.
                configured.capture(result, observed, registration.original_end)
                hosted._lifetime(registration.original_end, until)
            except BaseException as error:
                primary = _first_cancellation(primary, error)
                uncertain = True
            uncertain = uncertain or observed.locator_unknown
    except BaseException as error:
        primary = _first_cancellation(primary, error)
        uncertain = True
        # __enter__ can be interrupted after it installs our resolver; Python
        # then does not call __exit__. Retire only our still-owned local scope.
        # This is one bounded cleanup, never a second CLI/callback/wire attempt.
        if manager is not None and hosted._reviewed_hosted_cli is manager._resolver:
            try:
                manager.__exit__(type(error), error, error.__traceback__)
            except BaseException as retirement_error:
                primary = _first_cancellation(primary, retirement_error)
                uncertain = True
    if hosted._reviewed_hosted_cli is not before:
        uncertain = True  # Never overwrite/adopt somebody else's resolver.
    try:
        hosted._lifetime(registration.original_end, until)  # Includes outer retirement.
    except BaseException as error:
        primary = _first_cancellation(primary, error)
        uncertain = True
    if primary is not None and not _success(primary):
        raise primary
    if uncertain:
        return 78
    if primary is not None:
        raise primary  # Preserve the original successful SystemExit unchanged.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
