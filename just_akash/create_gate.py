"""Opt-in refusal of every plain (unauthorized) Console deployment create.

just-akash#332 requires every CI create to go through a durable, authorized
creation transaction. Until the SDK's own journal and broker exist, a consumer
that has moved to an authorized create path sets::

    JUST_AKASH_REQUIRE_AUTHORIZED_CREATE=1

and every plain create site in this package refuses BEFORE any network I/O, so
an un-migrated code path cannot quietly spend from a shared account.

The parse is strict on purpose. Unset or ``"0"`` is off and ``"1"`` is on.
Anything else (``"true"``, ``"yes"``, an empty string, a typo) refuses: a typo
that read as "off" would silently disable the one control a consumer asked for.

The gate state is logged once per create site, so a green run can never be read
as an authorized one.

This gate is not authorization. It only refuses. The authorized path
(:class:`just_akash.authorized_console.AuthorizedConsoleCreate`) does not use
``create_deployment`` and is unaffected by it.
"""

from __future__ import annotations

import logging
import os

GATE_ENV = "JUST_AKASH_REQUIRE_AUTHORIZED_CREATE"

logger = logging.getLogger(__name__)


class PlainCreateRefused(RuntimeError):
    """No deployment create was sent: plain creates are refused by configuration."""


def _announce(message: str) -> None:
    logger.info(message)
    print(message, flush=True)


def require_plain_create_allowed(site: str, *, announce: bool = True) -> None:
    """Refuse a plain create at ``site`` unless the gate is explicitly off.

    Must be called before the site performs ANY network I/O. Logs exactly one
    ``authorized-create gate:`` line per call, unless ``announce`` is False: the
    client-level backstop is silent so that each site still logs exactly one line.
    """
    say = _announce if announce else (lambda _message: None)
    raw = os.environ.get(GATE_ENV)
    if raw is None:
        say(f"authorized-create gate: off ({GATE_ENV} unset) at {site}")
        return
    if raw == "0":
        say(f"authorized-create gate: off ({GATE_ENV}=0) at {site}")
        return
    if raw == "1":
        say(f"authorized-create gate: on ({GATE_ENV}=1) at {site}: refusing plain create")
        raise PlainCreateRefused(
            f"{site}: plain deployment create refused because {GATE_ENV}=1. "
            "No create was sent. Use the authorized create path, or unset the "
            "variable (or set it to 0) to allow a plain create."
        )
    say(f"authorized-create gate: on (invalid {GATE_ENV} value) at {site}: refusing")
    raise PlainCreateRefused(
        f"{site}: {GATE_ENV} must be unset, '0' or '1'; refusing the plain create "
        "rather than reading an unrecognised value as off. No create was sent."
    )
