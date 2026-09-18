# Parser sample: a frozenset literal whose shape combines Constants, a splatted
# binding, and a multi-line block — the exact three shapes the walker must
# handle to give the right answer.
#
# This file is a FROZEN PARSER SAMPLE, not a vendored contract. It was copied
# once from blazing's scripts/cleanup_run_runner_registrations.py at
# commit 811a81815d970d69479db15e8b7dc5e6f78eb96d, and is intentionally not
# refreshed from the live source.
#
# The name and framing matter: a future reader must not conclude that "the
# walker test asserts 20 members" means "blazing's accept-list has 20 members
# today." It does not. The live cross-repo contract check lives in the #393
# membership test, which fetches the producer file at the branch head and
# walks it. THIS fixture only proves the walker resolves THIS shape correctly;
# the shape may drift from blazing and that drift is expected and harmless.
#
# If this fixture is updated, the update is a parser-sample change, not a
# contract change. The test that consumes it asserts the parsed set has 20
# unique members, which is a property of the walker plus this sample.

# fmt: off
PER_ATTEMPT_MINT_REASONS = frozenset({"RUNNER_SDL_TEMPLATE_MISSING", "RUNNER_SDL_UNRENDERED", "RUNNER_TOKEN_UNMINTED"})  # noqa: E501
ACCEPTED_ZERO_FAILURE_REASONS = frozenset(
    {
        "NO_ELIGIBLE_BIDDER",
        "RUNNER_PAT_INVALID",
        "RUNNER_PAT_MISSING",
        "WALLET_TX_CONTENTION",
        "WALLET_UNDERFUNDED",
        *PER_ATTEMPT_MINT_REASONS,
        # NEW: just-akash #398 ...
        "PROVIDER_OFFLINE",
        "PROVIDER_INVALID_VERSION",
        "PROVIDER_NO_CAPACITY",
        "PROVIDER_NO_BID",
        "PROVIDER_STATUS_QUERY_FAILED",
        "PROVIDER_UNKNOWN",
        "NO_BIDS_RECEIVED",
        "BIDS_MALFORMED",
        "BIDS_STALE",
        "BIDS_FOREIGN_ONLY",
        "SDL_ERROR",
        "CONFIG_ERROR",
    }
)
# fmt: on
