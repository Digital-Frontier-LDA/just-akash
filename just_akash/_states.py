"""Shared on-chain deployment/lease state constants.

State strings come from the Akash Console API and are checked in several modules
to decide whether a deployment is finished (and so holds no escrow). Keep them in
ONE place: a new terminal state — or a rename across Console API versions — is
added once here, not "kept in sync by comment" across ``_e2e.py`` and
``smoke_providers.py`` (the drift that motivated this module).

``_e2e._SETTLED_STATES`` and ``smoke_providers._DEAD_STATES`` are both this set.
``smoke_providers._LEASE_DOWN_STATES`` is a distinct, smoke-specific subset
(``failed``/``closed``) and stays local to that module.
"""

from __future__ import annotations

# A deployment is finished: it will not become active again and holds no escrow.
# Both spellings of "insufficient funds" appear in the wild across Console API
# versions, so both are recognised. A `closed` deployment reads escrow.state=closed
# with funds=0 (measured); `insufficient_funds` is terminal by definition (the
# escrow is what ran out).
TERMINAL_DEPLOYMENT_STATES: frozenset[str] = frozenset(
    {"closed", "failed", "insufficient_funds", "insufficientfunds"}
)
