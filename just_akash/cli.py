#!/usr/bin/env python3
"""
Unified CLI for just-akash.

Subcommands:
  deploy      — Deploy to Akash Network
  update      — Update a running deployment in place (no re-bid)
  connect     — SSH into a running deployment
  exec        — Execute a command on a running deployment
  inject      — Inject secrets into a running deployment via SSH
  logs        — Stream container logs from a deployment
  events      — Stream Kubernetes events for a deployment
  add-funds   — Add funds (USD) to a deployment's escrow
  auto-topup  — Show or set auto top-up for a deployment
  list        — List active deployments
  status      — Show deployment details
  destroy     — Destroy a deployment
  destroy-all — Destroy all deployments
  tag         — Tag a deployment with a name
  test        — End-to-end lifecycle test
  balance     — Show the wallet + deploy credit (--check --min-usd for alerting)
  lease-status — Reconcile lease/deployment/escrow state; flag closeable leases
  capacity-probe — Probe if N×GPU will actually place (a real bid, not /status)
  export-metrics — Render smoke telemetry as Prometheus textfile metrics
  runner-probe — Qualify providers as GitHub Actions runner HOSTS (takes real leases)
"""

import argparse
import logging
import math
import os
import shlex
import subprocess
import sys

NO_SSH_MSG = (
    "No SSH port found on this deployment.\n"
    "\n"
    "To use connect, exec, or inject via SSH, your SDL must:\n"
    "  1. Expose port 22 (SSH)\n"
    "  2. Include SSH_PUBKEY_B64 in the env block\n"
    "  3. Run sshd in the container entrypoint\n"
    "\n"
    "Use the SSH-enabled SDL:  just-akash deploy --sdl sdl/cpu-backtest-ssh.yaml\n"
    'Or set SSH_PUBKEY in .env: SSH_PUBKEY="ssh-ed25519 AAAA... your-key"\n'
    "\n"
    "Alternatively, use lease-shell transport (default in v1.5): no SSH required."
)


def _setup_logging():
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("AKASH_DEBUG") else logging.INFO,
        format="",
    )


def _require_api_key():
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    return api_key


def _resolve_deployment(client, dseq_arg):
    from .api import _extract_dseq, _interactive_pick, _resolve_dseq

    dseq = _resolve_dseq(dseq_arg)
    if not dseq:
        deployments = client.list_deployments()
        if not deployments:
            print("No active deployments.")
            sys.exit(1)
        dseq = (
            _extract_dseq(deployments[0])
            if len(deployments) == 1
            else _interactive_pick(deployments, client)
        )
    if not dseq:
        raise RuntimeError("No deployment selected")
    return dseq


def _resolve_deployment_client(dseq_arg):
    """Resolve a DSEQ and the configured Console wallet that positively owns it."""

    from .api import AkashConsoleAPI, _extract_dseq, _interactive_pick, _resolve_dseq
    from .wallet_pool import configured_api_keys, select_client_for_dseq

    dseq = _resolve_dseq(dseq_arg)
    if dseq:
        return select_client_for_dseq(dseq, client_factory=AkashConsoleAPI), dseq

    deployments = []
    owner_by_dseq = {}
    for key in configured_api_keys():
        client = AkashConsoleAPI(key)
        try:
            rows = client.list_deployments()
        except RuntimeError:
            continue
        for row in rows or []:
            found = _extract_dseq(row) if isinstance(row, dict) else None
            if found and found not in owner_by_dseq:
                owner_by_dseq[found] = client
                deployments.append(row)
    if not deployments:
        print("No active deployments across the configured Console wallet pool.")
        sys.exit(1)
    dseq = (
        _extract_dseq(deployments[0])
        if len(deployments) == 1
        else _interactive_pick(deployments, next(iter(owner_by_dseq.values())))
    )
    if not dseq or dseq not in owner_by_dseq:
        raise RuntimeError("No deployment selected")
    return owner_by_dseq[dseq], dseq


def _enrich_deployment_with_provider(client, deployment: dict) -> dict:
    """Inject provider hostUri into each lease so lease_shell transport can find it.

    The Console API /v1/deployments/{dseq} response stores the provider address as
    lease["id"]["provider"] but may omit (or leave blank) the hostUri. We resolve
    it from the provider registry and inject a "provider" dict in the shape
    LeaseShellTransport expects. Tolerant of unexpected API shapes.
    """
    leases = deployment.get("leases")
    if not isinstance(leases, list):
        return deployment
    for lease in leases:
        if not isinstance(lease, dict):
            continue
        lease_id = lease.get("id")
        provider_addr = lease_id.get("provider", "") if isinstance(lease_id, dict) else ""
        if not provider_addr:
            continue
        provider = lease.get("provider")
        existing_host = provider.get("hostUri") if isinstance(provider, dict) else None
        # Backfill when the provider dict is missing OR carries a blank hostUri,
        # so a registry-resolvable host isn't wrongly treated as "no host".
        if not existing_host:
            info = client.get_provider(provider_addr) or {}
            lease["provider"] = {"hostUri": info.get("hostUri", "")}
    return deployment


_SERVICE_HELP = (
    "Service (container) to target. Needed when the deployment has several "
    "services, or when the lease has not reported its service status yet "
    "(the Console API populates it lazily, so it can be empty even after a "
    "container is up). Skips inference entirely."
)


def _make_lease_shell(client, dseq):
    """Build a validated lease-shell transport for read-only streaming.

    Used by `logs` and `events`, which have no SSH equivalent. Returns the
    concrete LeaseShellTransport (so its stream_logs/stream_events are visible)
    and exits with a helpful message if the deployment has no active lease /
    provider hostUri.
    """
    from .transport.base import TransportConfig
    from .transport.lease_shell import LeaseShellTransport

    deployment = _enrich_deployment_with_provider(client, client.get_deployment(dseq))
    transport = LeaseShellTransport(
        TransportConfig(dseq=dseq, api_key=client.api_key, deployment=deployment)
    )
    if not transport.validate():
        print(
            "Error: no active lease / provider hostUri for this deployment yet.\n"
            "Logs and events stream from the provider, which requires an active "
            "lease. Check 'just-akash status' and try again once it's running.",
            file=sys.stderr,
        )
        sys.exit(1)
    return transport


def _require_ssh(client, dseq, key_arg):
    from .api import _build_ssh_cmd, _extract_ssh_info, _find_ssh_key

    deployment = client.get_deployment(dseq)
    ssh = _extract_ssh_info(deployment)
    if not ssh:
        print(f"Error: {NO_SSH_MSG}", file=sys.stderr)
        sys.exit(1)
    key_path = _find_ssh_key(key_arg)
    if not key_path:
        print("No SSH key found. Specify with --key")
        sys.exit(1)
    return ssh, _build_ssh_cmd(ssh, key_path)


def main():
    # ── runner-probe ───────────────────────────────────
    # Dispatched BEFORE the main parser, and deliberately not as a subparser.
    #
    # docs/github-runners.md and runner-pool.yml's RUNNER_NEVER_REGISTERED error both
    # tell the operator to run `just-akash runner-probe`, and it did not exist — the
    # remedy printed at the moment of failure was a command that errors out. Only
    # `python -m just_akash.runner_probe` worked, which nothing pointed at.
    #
    # Re-declaring its dozen flags here would put the real definition and a copy in two
    # files and let them drift, and the drift is silent: a probe invoked with a stale
    # default measures a different bar than the one documented. Handing argv straight to
    # the module keeps ONE definition, so `runner-probe --help` is its own help.
    if len(sys.argv) > 1 and sys.argv[1] == "runner-probe":
        from .runner_probe import main as runner_probe_main

        return sys.exit(runner_probe_main(sys.argv[2:], prog="just-akash runner-probe"))

    parser = argparse.ArgumentParser(
        prog="just-akash",
        description="CLI for deploying on Akash Network via the Console API",
        # runner-probe is dispatched above rather than registered as a subparser, so
        # argparse cannot list it. Without this it is invisible to `--help` — and the
        # whole point of adding the subcommand was that the remedy printed on a
        # RUNNER_NEVER_REGISTERED failure should be a command you can find and run.
        epilog=(
            "additional commands:\n"
            "  runner-probe        Qualify providers as GitHub Actions runner HOSTS.\n"
            "                      Takes REAL leases and spends REAL credit.\n"
            "                      See `just-akash runner-probe --help`.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── deploy ─────────────────────────────────────────
    deploy_p = subparsers.add_parser("deploy", help="Deploy to Akash Network")
    deploy_p.add_argument("--sdl", default="sdl/cpu-backtest.yaml", help="Path to SDL file")
    deploy_p.add_argument("--gpu", action="store_true", help="Use GPU variant SDL")
    deploy_p.add_argument("--image", default=None, help="Override container image")
    deploy_p.add_argument(
        "--bid-wait",
        type=int,
        default=60,
        help="Equal-opportunity auction window, 0-60 seconds (default: 60)",
    )
    deploy_p.add_argument(
        "--bid-wait-retry",
        type=int,
        default=120,
        help="Total deadline for first-bid fallback after the preferred window",
    )
    deploy_p.add_argument(
        "--env",
        action="append",
        dest="deploy_env_vars",
        default=[],
        help="KEY=VALUE env var to inject into SDL (repeatable, provider-visible)",
    )
    deploy_p.add_argument(
        "--provider",
        action="append",
        dest="preferred_providers",
        default=None,
        help="Preferred provider address (repeatable; overrides AKASH_PROVIDERS)",
    )
    deploy_p.add_argument(
        "--select",
        dest="select",
        choices=["cheapest", "emptiest"],
        default="cheapest",
        help=(
            "Bid selection policy among equally-eligible providers. cheapest (default) "
            "picks the lowest price; emptiest prefers the provider with the most free "
            "capacity, probing each bidder's /status. emptiest costs one HTTP round-trip "
            "per bidder and silently degrades to cheapest for any provider whose status "
            "is unreadable."
        ),
    )
    deploy_p.add_argument(
        "--backup-provider",
        action="append",
        dest="backup_providers",
        default=None,
        help="Backup provider address (repeatable; overrides AKASH_PROVIDERS_BACKUP)",
    )
    # A caller that must land on ONE named provider or not at all. Omitting
    # --backup-provider does NOT do this: _resolve_tier() only consults the arg when it is
    # not None, so an absent flag falls through to AKASH_PROVIDERS_BACKUP and the deploy
    # quietly gets the whole backup tier. That is exactly how the per-provider canary spent
    # weeks measuring itself -- it never passed --backup-provider, inherited 10 backups from
    # the environment, and landed on a provider it had not asked for.
    #
    # Clearing the env var in the caller would also work and is worse: it is invisible at the
    # call site, and anything that later exports AKASH_PROVIDERS_BACKUP silently re-arms the
    # fallback. The intent belongs in the command, where it can be read and tested.
    deploy_p.add_argument(
        "--no-backup-fallback",
        action="store_true",
        dest="no_backup_fallback",
        help="Fail if no PREFERRED provider bids, instead of falling back to the backup "
        "tier. Ignores AKASH_PROVIDERS_BACKUP entirely. For deployments whose identity "
        "IS the provider (the per-provider canary): one landing anywhere else measures "
        "nothing and is worse than a missing one, which at least alerts honestly.",
    )
    deploy_p.add_argument(
        "--deposit",
        type=float,
        default=5.0,
        help="Escrow deposit in USD (default: 5.0). Unused escrow is refunded "
        "when the deployment closes; size it to outlast the workload.",
    )

    # ── update ─────────────────────────────────────────
    update_p = subparsers.add_parser(
        "update", help="Update a running deployment in place (no re-bid)"
    )
    update_p.add_argument("--dseq", default="")
    update_p.add_argument("--sdl", required=True, help="Path to the revised SDL file")
    update_p.add_argument("--image", default=None, help="Override container image")
    update_p.add_argument(
        "--env",
        action="append",
        dest="update_env_vars",
        default=[],
        help="KEY=VALUE env var to inject into SDL (repeatable, provider-visible)",
    )

    # ── connect ────────────────────────────────────────
    connect_p = subparsers.add_parser(
        "connect", help="Open interactive shell on a running deployment"
    )
    connect_p.add_argument("--dseq", default="")
    connect_p.add_argument("--key", default="")
    connect_p.add_argument(
        "--transport",
        choices=["ssh", "lease-shell"],
        default="lease-shell",
        dest="transport",
        help="Transport to use: 'lease-shell' (default) or 'ssh'",
    )
    connect_p.add_argument(
        "--service",
        default="",
        help=_SERVICE_HELP,
    )

    # ── exec ───────────────────────────────────────────
    exec_p = subparsers.add_parser("exec", help="Execute a command on a running deployment")
    exec_p.add_argument("--dseq", default="")
    exec_p.add_argument("--key", default="")
    exec_p.add_argument(
        "--transport",
        choices=["ssh", "lease-shell"],
        default="lease-shell",
        dest="transport",
        help="Transport to use: 'lease-shell' (default) or 'ssh'",
    )
    exec_p.add_argument(
        "--service",
        default="",
        help=_SERVICE_HELP,
    )
    exec_p.add_argument("remote_cmd", help="Command to execute remotely")

    # ── benchmark ──────────────────────────────────────
    bench_p = subparsers.add_parser(
        "benchmark",
        help="Benchmark what a provider actually delivered (vCPU/RAM/disk/WAN)",
        description=(
            "Measure the hardware behind a lease: vCPU throughput, RAM bandwidth, disk "
            "I/O, WAN RTT, plus contention (PSI / cgroup throttling). The smoke test "
            "says whether a provider WORKS; this says whether it's any GOOD. Bounded "
            "well under the lease's limits (256M mem / 256M disk / 1 thread) so it can "
            "never OOM the container it is measuring. Run it on demand — not in the "
            "smoke path, where its load would make a feature failure unattributable."
        ),
    )
    bench_p.add_argument("--dseq", default="")
    bench_p.add_argument("--service", default="", help=_SERVICE_HELP)
    bench_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON object (for accruing/grading) instead of the table",
    )

    # ── inject ─────────────────────────────────────────
    inject_p = subparsers.add_parser("inject", help="Inject secrets into a running deployment")
    inject_p.add_argument("--dseq", default="")
    inject_p.add_argument("--key", default="")
    inject_p.add_argument(
        "--env",
        action="append",
        dest="env_vars",
        default=[],
        help="KEY=VALUE secret to inject (repeatable)",
    )
    inject_p.add_argument(
        "--env-file",
        dest="env_file",
        default="",
        help="Path to env file with secrets",
    )
    inject_p.add_argument(
        "--remote-path",
        dest="remote_path",
        default="/run/secrets/.env",
        help="Remote path to write secrets (default: /run/secrets/.env)",
    )
    inject_p.add_argument(
        "--transport",
        choices=["ssh", "lease-shell"],
        default="lease-shell",
        dest="transport",
        help="Transport to use: 'lease-shell' (default) or 'ssh'",
    )

    # ── logs ───────────────────────────────────────────
    logs_p = subparsers.add_parser("logs", help="Stream container logs from a deployment")
    logs_p.add_argument("--dseq", default="")
    logs_p.add_argument(
        "-f", "--follow", action="store_true", help="Stream continuously (Ctrl-C to stop)"
    )
    logs_p.add_argument(
        "--tail", type=int, default=100, help="Number of trailing lines to show (default: 100)"
    )
    logs_p.add_argument(
        "--service", default=None, help="Filter to a single service (default: all services)"
    )
    logs_p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after N seconds and return (bounded snapshot; avoids hanging "
        "when the provider holds a non-follow connection open).",
    )

    # ── events ─────────────────────────────────────────
    events_p = subparsers.add_parser(
        "events", help="Stream Kubernetes events for a deployment (debug startup)"
    )
    events_p.add_argument("--dseq", default="")
    events_p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after N seconds and return (bounded snapshot).",
    )

    # ── add-funds ──────────────────────────────────────
    add_funds_p = subparsers.add_parser(
        "add-funds", help="Add funds (USD) to a deployment's escrow"
    )
    add_funds_p.add_argument("--dseq", default="")
    add_funds_p.add_argument(
        "--deposit",
        type=float,
        required=True,
        help="Amount to add in USD (minimum 0.5)",
    )
    add_funds_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")

    # ── auto-topup ─────────────────────────────────────
    auto_topup_p = subparsers.add_parser(
        "auto-topup", help="Show or set auto top-up for a deployment"
    )
    auto_topup_p.add_argument("--dseq", default="")
    auto_topup_group = auto_topup_p.add_mutually_exclusive_group()
    auto_topup_group.add_argument("--on", action="store_true", help="Enable auto top-up")
    auto_topup_group.add_argument("--off", action="store_true", help="Disable auto top-up")

    # ── list ───────────────────────────────────────────
    list_p = subparsers.add_parser("list", help="List active deployments")
    list_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # ── balance ────────────────────────────────────────
    balance_p = subparsers.add_parser(
        "balance",
        help="Show the Console-API wallet and its remaining deploy credit",
    )
    balance_p.add_argument("--json", action="store_true", help="Output in JSON format")
    balance_p.add_argument(
        "--check",
        action="store_true",
        help="Threshold mode: print a machine-readable verdict and exit non-zero when "
        "deploy credit is below --min-usd (so a scheduled job can flag a low wallet "
        "BEFORE deploys start 402ing).",
    )
    balance_p.add_argument(
        "--min-usd",
        type=float,
        default=None,
        metavar="N",
        help="Minimum acceptable deploy credit in USD for --check.",
    )

    # ── export-metrics ─────────────────────────────────
    export_p = subparsers.add_parser(
        "export-metrics",
        help="Render smoke telemetry JSONL as Prometheus textfile-collector metrics",
    )
    export_p.add_argument("jsonl", help="Path to the smoke telemetry JSONL file")
    export_p.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Write metrics atomically to FILE (default: stdout)",
    )
    export_p.add_argument(
        "--benchmark",
        default=None,
        metavar="FILE",
        help="Also render just_akash_bench_* gauges from this smoke-benchmark.jsonl",
    )
    export_credit_group = export_p.add_mutually_exclusive_group()
    export_credit_group.add_argument(
        "--with-credit",
        action="store_true",
        help="Also emit just_akash_deploy_credit_usd from on-chain credit (needs AKASH_API_KEY)",
    )
    export_credit_group.add_argument(
        "--credit-json",
        default=None,
        metavar="FILE",
        help="Also emit just_akash_deploy_credit_usd from a `balance --check --json` "
        "snapshot file (no API key needed)",
    )

    # ── status ─────────────────────────────────────────
    status_p = subparsers.add_parser("status", help="Show deployment details")
    status_p.add_argument("--dseq", default="")
    status_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # ── destroy ────────────────────────────────────────
    destroy_p = subparsers.add_parser("destroy", help="Destroy a deployment")
    destroy_p.add_argument("--dseq", default="")
    destroy_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")

    # ── destroy-all ────────────────────────────────────
    destroy_all_p = subparsers.add_parser("destroy-all", help="Destroy all deployments")
    destroy_all_p.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompts"
    )

    # ── tag ────────────────────────────────────────────
    tag_p = subparsers.add_parser("tag", help="Tag a deployment with a name")
    tag_p.add_argument("--dseq", required=True)
    tag_p.add_argument("--name", required=True)

    # ── test ────────────────────────────────────────────
    test_p = subparsers.add_parser("test", help="End-to-end lifecycle test")
    test_p.add_argument("--sdl", default="sdl/cpu-backtest-ssh.yaml")
    test_p.add_argument(
        "--bid-wait", type=int, default=240, help="Total wait timeout for test (default: 240)"
    )
    test_p.add_argument("--ssh", action="store_true", help="Verify SSH connectivity")

    # ── lease-remaining ────────────────────────────────
    lr_p = subparsers.add_parser(
        "lease-remaining",
        help="Estimate how long the escrow lasts at the current burn rate",
    )
    lr_p.add_argument("--dseq", default="", help="Deployment DSEQ (auto-selects if omitted)")
    lr_p.add_argument("--json", action="store_true", help="Output in JSON format")
    lr_p.add_argument(
        "--block-time",
        type=float,
        default=None,
        help="Block time in seconds (default: 6.0; env AKASH_BLOCK_TIME_S)",
    )

    # ── unleased-orders ────────────────────────────────
    uo_p = subparsers.add_parser(
        "unleased-orders",
        help="Deployments still holding escrow whose order never acquired a lease "
        "(report-only; verdicts come from akash-lease-core's leaked-order policy)",
    )
    uo_p.add_argument("--owner", required=True, help="Akash account address to audit")
    uo_p.add_argument("--json", action="store_true", help="Output in JSON format")
    uo_p.add_argument(
        "--min-age-seconds",
        type=float,
        default=None,
        help="Override the age floor. ⚠ The default (900s) is DERIVED in "
        "akash-lease-core from the 450s bid window x2 — below it an order may still be "
        "mid-auction, which is how the previous version of this audit produced five "
        "false positives. Lower it only with a reason.",
    )

    # ── lease-status ───────────────────────────────────
    ls_p = subparsers.add_parser(
        "lease-status",
        help="Reconcile chain lease + deployment + escrow state across all your leases "
        "(authoritative 'which leases are live / closeable', independent of a provider's "
        "self-reported /status)",
    )
    ls_p.add_argument("--json", action="store_true", help="Output in JSON format")
    ls_p.add_argument(
        "--all",
        action="store_true",
        dest="include_closed",
        help="Include closed/terminal deployments too (default: active deployments only).",
    )
    ls_p.add_argument(
        "--closeable-only",
        action="store_true",
        help="Show only leases flagged closeable (terminal state or drained escrow) — "
        "the set worth closing to stop escrow bleed.",
    )

    # ── orphan-scan ────────────────────────────────────
    orph_p = subparsers.add_parser(
        "orphan-scan",
        help="Find deployments that hold escrow and will NEVER get a lease, classified "
        "from authoritative on-chain ORDER state (an open order is the only path to a "
        "lease). Report-only: it destroys nothing.",
    )
    orph_p.add_argument("--json", action="store_true", help="Output in JSON format")
    orph_p.add_argument(
        "--reapable-only",
        action="store_true",
        help="Show only orphans confirmed by >=2 independent LCD endpoints — the set a "
        "reaper could act on. A single endpoint is a reading, not a confirmation.",
    )

    # ── capacity-probe ─────────────────────────────────
    cap_p = subparsers.add_parser(
        "capacity-probe",
        help="Probe whether N×GPU will actually place right now — a real bid, not the "
        "provider /status inventory (creates a throwaway order, reads bids, closes it "
        "without leasing)",
    )
    cap_p.add_argument("--gpu-count", type=int, default=1, help="GPUs to request (default: 1)")
    cap_p.add_argument(
        "--gpu-model",
        default=None,
        metavar="M",
        help="GPU model to pin (e.g. v100, rtx4000ada, t4). Omit for any NVIDIA GPU.",
    )
    cap_p.add_argument(
        "--wait", type=int, default=45, help="Seconds to poll for bids (default: 45)"
    )
    cap_p.add_argument(
        "--provider", default=None, metavar="ADDR", help="Only report bids from this provider"
    )
    cap_p.add_argument("--json", action="store_true", help="Output in JSON format")

    # ── validate-sdl ───────────────────────────────────
    validate_p = subparsers.add_parser(
        "validate-sdl",
        help="Check an SDL against project rules without deploying",
    )
    validate_p.add_argument("sdl", help="Path to SDL file")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    _setup_logging()

    # ── deploy ─────────────────────────────────────────
    if args.command == "deploy":
        from .deploy import _resolve_tier, deploy

        # Two flags that contradict each other must not resolve silently in favour of one.
        # Ignoring the explicit --backup-provider would do the safer thing while telling the
        # caller nothing, and a caller who wrote both does not know what they are getting.
        if args.no_backup_fallback and args.backup_providers:
            print(
                "Error: --no-backup-fallback and --backup-provider contradict each other. "
                f"--no-backup-fallback means 'the preferred provider or nothing', but "
                f"{len(args.backup_providers)} backup provider(s) were also given. "
                "Drop one.",
                file=sys.stderr,
            )
            sys.exit(2)
        # --no-backup-fallback with NO preferred tier would INVERT its own promise. deploy()
        # computes has_allowlist = bool(preferred or backup); emptying backup while preferred
        # is also empty makes that False, which means "no allowlist -- accept a bid from ANY
        # provider". The flag would then take a deploy that was constrained to the backup tier
        # and let it land literally anywhere, spending escrow on a provider nobody named.
        #
        # Resolved with _resolve_tier rather than by testing args.preferred_providers, because
        # the preferred tier may come from AKASH_PROVIDERS. Reusing the real resolver keeps
        # this guard from drifting out of step with the semantics it is guarding.
        # Raised by Copilot on #145.
        if args.no_backup_fallback and not _resolve_tier(
            args.preferred_providers, "AKASH_PROVIDERS"
        ):
            print(
                "Error: --no-backup-fallback requires a preferred provider, and none is "
                "configured. Pass --provider or set AKASH_PROVIDERS. Without one there is "
                "no allowlist at all, so the deploy would accept a bid from ANY provider -- "
                "the opposite of what this flag promises.",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            deploy(
                sdl_path=args.sdl,
                gpu=args.gpu,
                image=args.image,
                bid_wait=args.bid_wait,
                bid_wait_retry=args.bid_wait_retry,
                env_vars=args.deploy_env_vars,
                preferred_providers=args.preferred_providers,
                # [] and None are NOT the same to _resolve_tier: [] means "no backups, do not
                # look at the environment", None means "no opinion, read AKASH_PROVIDERS_BACKUP".
                # The flag has to produce the former, which is why it cannot just leave the
                # argument unset.
                backup_providers=[] if args.no_backup_fallback else args.backup_providers,
                deposit=args.deposit,
                select=args.select,
            )
            sys.exit(0)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── update ─────────────────────────────────────────
    elif args.command == "update":
        from .deploy import update

        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            update(
                dseq=dseq,
                sdl_path=args.sdl,
                image=args.image,
                env_vars=args.update_env_vars,
                api_key=client.api_key,
            )
            sys.exit(0)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── connect ────────────────────────────────────────
    elif args.command == "connect":
        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            use_lease_shell = args.transport == "lease-shell"
            if use_lease_shell:
                from .transport import make_transport

                deployment = _enrich_deployment_with_provider(client, client.get_deployment(dseq))
                transport = make_transport(
                    "lease-shell",
                    dseq=dseq,
                    api_key=client.api_key,
                    deployment=deployment,
                    service_name=args.service or None,
                )
                if not transport.validate():
                    print(
                        "Notice: lease-shell transport is not available for this deployment "
                        "(no active lease or provider hostUri missing). Falling back to SSH.",
                        file=sys.stderr,
                    )
                    use_lease_shell = False
            if use_lease_shell:
                transport.prepare()
                transport.connect()
            else:
                ssh, ssh_cmd = _require_ssh(client, dseq, args.key)
                print(f"Connecting to {ssh['host']}:{ssh['port']}...")
                os.execvp("ssh", ssh_cmd)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── exec ───────────────────────────────────────────
    elif args.command == "exec":
        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            use_lease_shell = args.transport == "lease-shell"
            if use_lease_shell:
                from .transport import make_transport

                deployment = _enrich_deployment_with_provider(client, client.get_deployment(dseq))
                transport = make_transport(
                    "lease-shell",
                    dseq=dseq,
                    api_key=client.api_key,
                    deployment=deployment,
                    service_name=args.service or None,
                )
                if not transport.validate():
                    print(
                        "Notice: lease-shell transport is not available for this deployment "
                        "(no active lease or provider hostUri missing). Falling back to SSH.",
                        file=sys.stderr,
                    )
                    use_lease_shell = False
            if use_lease_shell:
                transport.prepare()
                rc = transport.exec(args.remote_cmd)
                sys.exit(rc)
            else:
                ssh, ssh_cmd = _require_ssh(client, dseq, args.key)
                ssh_cmd.append(args.remote_cmd)
                print(f"Executing on {ssh['host']}:{ssh['port']}...")
                # `exec` runs a user-supplied command on the user's own deployment
                # by design (this is `ssh host <cmd>`); the command is the feature.
                result = subprocess.run(ssh_cmd, text=True)
                sys.exit(result.returncode)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── benchmark ──────────────────────────────────────
    elif args.command == "benchmark":
        import io
        import json as _json

        from .api import AkashConsoleAPI, _extract_lease_provider
        from .benchmark import (
            BENCH_SH,
            build_json_record,
            format_results,
            is_complete,
            parse_results,
        )

        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            from .transport import make_transport

            deployment = _enrich_deployment_with_provider(client, client.get_deployment(dseq))
            transport = make_transport(
                "lease-shell",
                dseq=dseq,
                api_key=client.api_key,
                deployment=deployment,
                service_name=args.service or None,
            )
            if not transport.validate():
                print(
                    "Error: lease-shell is not available for this deployment (no active "
                    "lease or provider hostUri missing) — cannot benchmark.",
                    file=sys.stderr,
                )
                sys.exit(1)
            transport.prepare()
            # The probe writes its BENCH- lines to the command's stdout, which the
            # transport streams straight to ours; capture it instead so we can parse.
            cap = io.BytesIO()

            class _Capture:
                buffer = cap

                def write(self, *_a):
                    pass

                def flush(self):
                    pass

            # BENCH_SH is a shell SCRIPT — it must run via `sh -c`, not exec()'s argv
            # path (which returns $()/pipes/`;` literal and yields no output). That
            # method lives on LeaseShellTransport; assert rather than type: ignore, so
            # a future factory change fails loudly here instead of silently.
            from .transport.lease_shell import LeaseShellTransport

            assert isinstance(transport, LeaseShellTransport)  # noqa: S101
            real_stdout = sys.stdout
            sys.stdout = _Capture()  # type: ignore[assignment]
            try:
                transport.exec_shell_script(BENCH_SH)
            finally:
                sys.stdout = real_stdout
            results = parse_results(cap.getvalue().decode("utf-8", errors="replace"))
            # Reuse the shared, edge-case-tested lease-provider extractor rather than
            # re-parsing the lease shape here (avoids drift on odd Console payloads).
            provider = _extract_lease_provider(deployment) or ""
            if args.as_json:
                # build_json_record spreads the trusted fields last so a hostile probe
                # can't shadow dseq/provider/complete (unit-tested in test_benchmark).
                print(_json.dumps(build_json_record(dseq, provider, results)))
            else:
                print(format_results(provider or f"dseq {dseq}", results))
            # A partial sample must not be graded as if it were a full one.
            sys.exit(0 if is_complete(results) else 1)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "inject":
        try:
            client, dseq = _resolve_deployment_client(args.dseq)

            env_lines: list[str] = []
            for pair in args.env_vars:
                if "=" not in pair:
                    print(f"Error: Invalid --env format: {pair!r} (expected KEY=VALUE)")
                    sys.exit(1)
                env_lines.append(pair)

            if args.env_file:
                from pathlib import Path

                env_file_path = Path(args.env_file)
                if not env_file_path.exists():
                    print(f"Error: Env file not found: {args.env_file}")
                    sys.exit(1)
                for line in env_file_path.read_text().splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        env_lines.append(stripped)

            if not env_lines:
                print("Error: No secrets to inject. Use --env KEY=VALUE or --env-file PATH")
                sys.exit(1)

            use_lease_shell = args.transport == "lease-shell"
            if use_lease_shell:
                from .transport import make_transport

                deployment = _enrich_deployment_with_provider(client, client.get_deployment(dseq))
                transport = make_transport(
                    "lease-shell",
                    dseq=dseq,
                    api_key=client.api_key,
                    deployment=deployment,
                )
                if not transport.validate():
                    print(
                        "Notice: lease-shell transport is not available for this deployment "
                        "(no active lease or provider hostUri missing). Falling back to SSH.",
                        file=sys.stderr,
                    )
                    use_lease_shell = False
            if use_lease_shell:
                secrets_content = "\n".join(env_lines) + "\n"
                transport.prepare()
                transport.inject(args.remote_path, secrets_content)
                print(f"Injected {len(env_lines)} secret(s) into {dseq}:{args.remote_path}")
            else:
                ssh, ssh_cmd = _require_ssh(client, dseq, args.key)
                remote_path = args.remote_path
                # Quote the user-supplied path before it reaches the remote
                # shell (ssh runs the trailing arg via /bin/sh), matching the
                # lease-shell transport. Prevents metacharacters in --remote-path
                # from being interpreted remotely.
                quoted_path = shlex.quote(remote_path)
                secrets_content = "\n".join(env_lines) + "\n"

                mkdir_cmd = ssh_cmd + [f"mkdir -p $(dirname {quoted_path})"]
                result = subprocess.run(mkdir_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"Error creating remote directory: {result.stderr.strip()}")
                    sys.exit(1)

                write_cmd = ssh_cmd + [f"cat > {quoted_path}"]
                result = subprocess.run(
                    write_cmd, input=secrets_content, capture_output=True, text=True
                )
                if result.returncode != 0:
                    print(f"Error writing secrets: {result.stderr.strip()}")
                    sys.exit(1)

                chmod_cmd = ssh_cmd + [f"chmod 600 {quoted_path}"]
                subprocess.run(chmod_cmd, capture_output=True, text=True)

                print(f"Injected {len(env_lines)} secret(s) into {dseq}:{remote_path}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── list ───────────────────────────────────────────
    elif args.command == "list":
        from .api import AkashConsoleAPI, format_deployments_json, format_deployments_table

        try:
            client = AkashConsoleAPI(_require_api_key())
            use_json = args.json or not sys.stdout.isatty()
            deployments = client.list_deployments()
            if use_json:
                print(format_deployments_json(deployments))
            else:
                print(format_deployments_table(deployments))
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── balance ────────────────────────────────────────
    elif args.command == "balance":
        import json

        from . import chain
        from .api import AkashConsoleAPI, escrow_locked

        try:
            client = AkashConsoleAPI(_require_api_key())
            use_json = args.json or not sys.stdout.isatty()
            address = client.account_address()

            # Threshold mode: a scheduled low-credit alarm. Print a machine-readable
            # verdict and exit non-zero when the remaining deploy credit is under
            # --min-usd, so a cron/CI job flags "wallet low" BEFORE deploys start
            # returning HTTP 402. uact (Akash Credit Token) is the USD-pegged Console
            # deploy currency, so its USD estimate is exact (1e6 uact = $1).
            if args.check:
                if args.min_usd is None:
                    print("Error: balance --check requires --min-usd N", file=sys.stderr)
                    sys.exit(2)
                granted_uact = chain.granted_uact(address)
                if granted_uact is None:
                    unknown = {
                        "check": "deploy_credit",
                        "status": "UNKNOWN",
                        "account": address,
                        "reason": "canonical spend_limits quorum unavailable",
                        "min_usd": args.min_usd,
                    }
                    if use_json:
                        print(json.dumps(unknown))
                    else:
                        print(
                            "CREDIT-CHECK UNKNOWN"
                            f" reason={unknown['reason']} min_usd={args.min_usd:.2f}"
                            f" account={address}"
                        )
                    sys.exit(1)
                # Check FREE credit, not the grant. The DepositAuthorization
                # spend_limit is ALREADY NET of locked escrow (the Cosmos authz
                # module decrements it as the grantee uses escrow) — so
                # `granted_uact` from `chain.granted_uact()` IS the free credit,
                # and subtracting `locked_uact` a second time would double-count.
                # ⭐ Fix for #169: `free_uact = max(granted_uact - locked_uact, 0)`
                # was the OLD expression and was wrong. The fix removes the
                # subtraction. The locked_uact value is still useful as a
                # display field (`locked_in_escrow_usd`) — it just isn't a
                # subtrahend of free credit.
                escrow = escrow_locked(client)
                locked_uact = escrow["locked_uact"]
                free_uact = chain.free_uact(granted_uact)
                granted_usd = chain.usd_estimate("uact", granted_uact) or 0.0
                locked_usd = chain.usd_estimate("uact", locked_uact) or 0.0
                usd = chain.usd_estimate("uact", free_uact) or 0.0
                low = usd < args.min_usd
                # ⭐ Fix for #169 follow-on (CodeRabbit on #190): `spend_limits` IS
                # the deployable credit, so free_usd is no longer an UPPER bound —
                # escrow incompleteness cannot make free_usd a lie. The OLD code
                # downgraded OK to UNKNOWN on omission because, under the OLD
                # `free = granted - locked` formula, an incomplete escrow tally
                # made `locked` a lower bound and therefore `free` an upper bound.
                # Under NET semantics that concern is gone: omission is a
                # data-quality diagnostic, not a gate.
                #
                # The diagnostic is still emitted in the payload (`unreadable` +
                # `unnameable` counters) so an operator can see the tally was
                # incomplete — but it does NOT change the status, because
                # `spend_limits` is the same value whether or not we could read
                # any given deployment's escrow account.
                unreadable = escrow.get("unreadable", 0)
                unnameable = escrow.get("skipped_no_dseq", 0)
                omitted = unreadable + unnameable
                status = "LOW" if low else "OK"
                if use_json:
                    print(
                        json.dumps(
                            {
                                "check": "deploy_credit",
                                "status": status,
                                "account": address,
                                # free_usd is the gating value; the other two explain it.
                                "deploy_credit_usd": usd,
                                "free_usd": usd,
                                "granted_usd": granted_usd,
                                "locked_in_escrow_usd": locked_usd,
                                # Diagnostic only — does NOT affect status under
                                # NET semantics (spend_limits IS the free credit).
                                "escrow_unreadable_deployments": unreadable,
                                # Same meaning, different cause: a deployment with no
                                # extractable dseq is omitted from the tally too.
                                "escrow_unnameable_deployments": unnameable,
                                "min_usd": args.min_usd,
                            }
                        )
                    )
                else:
                    diag = (
                        f" ({omitted} escrow detail(s) omitted: "
                        f"{unreadable} unreadable, {unnameable} unnameable)"
                        if omitted
                        else ""
                    )
                    print(
                        f"CREDIT-CHECK status={status} free_usd={usd:.2f}"
                        f"{diag} (granted={granted_usd:.2f} "
                        f"locked_in_escrow={locked_usd:.2f}) "
                        f"min_usd={args.min_usd:.2f} account={address}"
                    )
                sys.exit(1 if low else 0)

            # Deploy credit is the real "wallet balance": Console holds the funds and
            # grants this account an escrow DepositAuthorization whose spend_limits is
            # what's left to spend. Liquid bank balance is usually empty (funds live as
            # the grant, not AKT). Both are read straight from the public chain.
            granted_uact_value = chain.granted_uact(address)
            if granted_uact_value is None:
                reason = "canonical spend_limits quorum unavailable"
                if use_json:
                    print(
                        json.dumps(
                            {
                                "account": address,
                                "status": "UNKNOWN",
                                "reason": reason,
                            }
                        )
                    )
                else:
                    print(f"CREDIT-CHECK UNKNOWN reason={reason} account={address}")
                return
            granted = {"uact": granted_uact_value}
            credit = chain.describe_coins(granted)
            liquid = chain.describe_coins(chain.bank_balances(address))
            grant = chain.credit_grant_detail(address)
            # `granted_uact` is `spend_limits` from the DepositAuthorization — ALREADY NET
            # of locked escrow (Cosmos authz decrements it as the grantee uses
            # escrow; see chain.free_uact's docstring). ⭐ Fix for #169: the OLD
            # expression `max(granted_uact - locked_uact, 0)` double-subtracts
            # and reads 0 for a funded wallet when locked > granted. The fix:
            # `free_uact = granted_uact` (via `chain.free_uact`).
            # `locked_in_escrow_uact` and `active_deployments` are still emitted
            # in the payload — they are useful diagnostic fields, just not
            # subtrahends of free credit.
            locked_info = escrow_locked(client)
            granted_uact = granted.get("uact", 0)
            locked_uact = locked_info["locked_uact"]
            free_uact = chain.free_uact(granted_uact)

            if use_json:
                print(
                    json.dumps(
                        {
                            "account": address,
                            "deploy_credit": credit,
                            "granted_uact": granted_uact,
                            "locked_in_escrow_uact": locked_uact,
                            "free_uact": free_uact,
                            "free_usd": chain.usd_estimate("uact", free_uact),
                            "active_deployments": locked_info["deployments"],
                            "liquid": liquid,
                            "credit_grant": grant,
                            "rest_url": chain.rest_url(),
                        },
                        indent=2,
                    )
                )
            else:
                print("Akash Console wallet")
                print(f"  account:        {address}")
                if credit:
                    lead = credit[0]
                    usd = f"  (≈ ${lead['usd_estimate']:,.2f})" if lead["usd_estimate"] else ""
                    print(f"  deploy credit:  {lead['display']}{usd}   (granted)")
                    for row in credit[1:]:
                        print(f"                  {row['display']}")
                    # Free is the number that predicts whether the next deploy works.
                    free_usd = chain.usd_estimate("uact", free_uact) or 0.0
                    n = locked_info["deployments"]
                    print(
                        f"  in escrow:      {chain.format_amount('uact', locked_uact)}"
                        f"   ({n} active deployment{'s' if n != 1 else ''})"
                    )
                    print(
                        f"  FREE to spend:  {chain.format_amount('uact', free_uact)}"
                        f"  (≈ ${free_usd:,.2f})"
                    )
                    _omitted = locked_info["unreadable"] + locked_info.get("skipped_no_dseq", 0)
                    if _omitted:
                        print(
                            f"                  note: {_omitted} deployment(s) omitted "
                            f"({locked_info['unreadable']} unreadable, "
                            f"{locked_info.get('skipped_no_dseq', 0)} unnameable) "
                            "— locked is a lower bound, free an upper bound"
                        )
                else:
                    print("  deploy credit:  none (no DepositAuthorization grant found)")
                if liquid:
                    print(f"  liquid on-chain: {', '.join(r['display'] for r in liquid)}")
                else:
                    print("  liquid on-chain: none")
                if grant:
                    exp = (grant.get("expiration") or "")[:10] or "no expiry"
                    print(f"  credit grant:   from {grant.get('granter')} (expires {exp})")
                print(f"  source:         {chain.rest_url()}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── orphan-scan ────────────────────────────────────
    elif args.command == "orphan-scan":
        import json

        from .api import AkashConsoleAPI, lease_status
        from .orphan_detect import Classification, FleetReport, classify_deployment

        try:
            client = AkashConsoleAPI(_require_api_key())
            use_json = args.json or not sys.stdout.isatty()
            owner = client.account_address()
            rows = lease_status(client, active_only=True)

            report = FleetReport()
            for r in rows:
                report.verdicts.append(
                    classify_deployment(
                        str(r.get("dseq")),
                        owner,
                        deployment_state=str(r.get("deployment_state", "")),
                        # ACTIVE leases only. `lease_count` counts every lease on the
                        # record INCLUDING closed ones, and a deployment whose lease
                        # closed while the deployment stayed open is precisely the
                        # orphan this scan exists to find — so passing the raw count
                        # classified it LEASED and reported zero orphans. Measured
                        # 2026-08-22: 26 deployments, no active lease, no open order,
                        # $104.33 held for ~45h, and `akash_canary_orphans_total` read
                        # 0 with `..._scan_degraded` 0 — a clean, complete, WRONG
                        # all-clear of exactly the kind canary/orphans.py was written
                        # to refuse.
                        console_lease_count=int(r.get("active_lease_count", 0) or 0),
                        escrow_uact=int(r.get("escrow_remaining_uact", 0) or 0),
                    )
                )

            # An UNKNOWN row is not a clean row. Surfacing it at fleet level stops a
            # caller that reads only `orphaned` from treating a half-read fleet as
            # healthy — the exact false-clean this command exists to refuse.
            unread = [v for v in report.verdicts if v.classification is Classification.UNKNOWN]
            if unread:
                report.degraded.append(
                    f"{len(unread)} deployment(s) could not be classified from the chain"
                )

            shown = (
                [v for v in report.orphaned if v.reapable]
                if args.reapable_only
                else report.verdicts
            )

            if use_json:
                print(
                    json.dumps(
                        {
                            "account": owner,
                            "degraded": report.is_degraded,
                            "degraded_reasons": report.degraded,
                            "orphaned_count": len(report.orphaned),
                            "orphaned_escrow_uact": report.orphaned_escrow_uact,
                            "unknown_count": len(report.unknown),
                            "deployments": [
                                {
                                    "dseq": v.dseq,
                                    "classification": v.classification.value,
                                    "escrow_uact": v.escrow_uact,
                                    "live_orders": v.live_orders,
                                    "confirmations": v.confirmations,
                                    "reapable": v.reapable,
                                    "detail": v.detail,
                                }
                                for v in shown
                            ],
                        },
                        indent=2,
                    )
                )
            else:
                for v in shown:
                    flag = "  <- reapable" if v.reapable else ""
                    print(
                        f"  {v.classification.value:<12} {v.dseq}  "
                        f"{v.escrow_uact / 1e6:>8.2f} ACT  {v.detail}{flag}"
                    )
                print(report.summary())

            # A degraded report must NOT exit 0: a caller checking only the exit status
            # would otherwise read an incomplete scan as a clean fleet.
            if report.is_degraded:
                sys.exit(1)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── lease-status ───────────────────────────────────
    elif args.command == "unleased-orders":
        import json as _json

        from akash_lease_core.orders import OrderPolicy

        from .unleased_orders import audit_owner, summarise

        policy = (
            OrderPolicy(min_age_seconds=args.min_age_seconds)
            if args.min_age_seconds is not None
            else None
        )
        try:
            decisions = audit_owner(args.owner, policy=policy)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        counts = summarise(decisions)
        closeable = [d for d in decisions if d.status.value == "closeable"]
        if args.json:
            print(
                _json.dumps(
                    {
                        "owner": args.owner,
                        "counts": counts,
                        "closeable": [d.dseq for d in closeable],
                    },
                    indent=2,
                )
            )
        else:
            print(f"owner {args.owner}")
            # ⚠ Print EVERY status, including the zeros. A summary that omits absent
            #   categories reads as "none of those exist" when it means "not shown".
            for status in (
                "closeable",
                "has_lease",
                "too_young",
                "protected",
                "excluded",
                "not_active",
                "not_open_order",
                "undetermined",
            ):
                print(f"  {status:16s} {counts.get(status, 0)}")
            for d in closeable:
                print(f"  ⚠ CLOSEABLE dseq={d.dseq}")
            if not closeable:
                # ⛔ Say which question was answered. "0 closeable" is only meaningful
                #   alongside the population it was drawn from.
                print(
                    f"\n⇒ no unleased orders over the age floor, across "
                    f"{sum(counts.values())} active deployment(s)."
                )
        # Report-only: a CLOSEABLE verdict is a CANDIDATE, never an authorisation.
        sys.exit(0)

    elif args.command == "lease-status":
        import json

        from . import chain
        from .api import AkashConsoleAPI, lease_status

        try:
            client = AkashConsoleAPI(_require_api_key())
            use_json = args.json or not sys.stdout.isatty()
            address = client.account_address()
            rows = lease_status(client, active_only=not args.include_closed)
            # ⚠ Corroborate against the UNFILTERED set. `--closeable-only` legitimately
            #   empties `rows`, and treating that as a degraded listing would fire on
            #   every healthy fleet with nothing to close.
            all_rows = list(rows)
            if args.closeable_only:
                rows = [r for r in rows if r["closeable"]]
            n_close = sum(1 for r in rows if r["closeable"])

            # ⛔⛔ AN EMPTY CONSOLE LISTING AND A CLEAN FLEET PRINT THE SAME THING.
            # `lease_status` builds its rows from `client.list_deployments()` — the
            # Console API. When that returns nothing, `closeable_count: 0` and
            # `leases: []` are emitted, which is byte-identical to a genuinely idle
            # account. Measured 2026-08-25: this command reported 0 leases for
            # akash1n4uut3v… while the chain showed 55 ACTIVE leases and 60 active
            # deployments for that same owner, in the same minute.
            #
            # ⚠ `orphan-scan` already solved this in this repo and states the rule:
            # "publishing 0 from a degraded scan would be a false all-clear … a green
            # number standing in for an unasked question". It carries `degraded` /
            # `degraded_reasons` and refuses to exit 0 on an incomplete scan. This
            # command carried no such field, so it could answer "nothing to close"
            # without having seen anything.
            #
            # THE CORROBORATION IS CHEAP AND CREDENTIAL-FREE: the chain already knows
            # how many active deployments an owner holds. A Console listing that is
            # empty while the chain is not is the degraded signal.
            #
            # ⚠ An UNREADABLE chain is NOT degraded. The primary source answered; we
            # simply could not confirm it. Reporting that as degraded would make every
            # LCD hiccup look like a Console failure.
            degraded_reasons: list[str] = []
            chain_active = chain.active_deployment_count(address)
            # The decision is a pure function so it can be tested on all four cases;
            # inline, it was only reachable through a live Console + chain read.
            degraded_reasons += chain.corroborate_listing(
                listing_is_empty=not all_rows, chain_active=chain_active, address=address
            )
            is_degraded = bool(degraded_reasons)

            def _esc(r):
                micro = r["escrow_remaining_uact"]
                return "?" if micro is None else chain.format_amount("uact", micro)

            if use_json:
                print(
                    json.dumps(
                        {
                            "account": address,
                            "scope": "all" if args.include_closed else "active",
                            "closeable_count": n_close,
                            "degraded": is_degraded,
                            "degraded_reasons": degraded_reasons,
                            "leases": rows,
                        },
                        indent=2,
                    )
                )
            elif not rows:
                scope = "" if args.include_closed else " active"
                only = "closeable " if args.closeable_only else ""
                print(f"No {only}{scope} leases for {address}")
            else:
                print(f"Leases for {address}")
                print(f"  {'DSEQ':<14} {'LEASE':<13} {'DEPLOY':<10} {'ESCROW LEFT':>13}  PROVIDER")
                for r in rows:
                    flag = "  ⚠ closeable" if r["closeable"] else ""
                    print(
                        f"  {str(r['dseq']):<14} {str(r['lease_state'] or '-'):<13} "
                        f"{str(r['deployment_state'] or '?'):<10} {_esc(r):>13}  "
                        f"{str(r['provider'] or 'no lease')}{flag}"
                    )
                if n_close:
                    print(
                        f"\n  {n_close} lease(s) closeable (terminal state or drained escrow) — "
                        "`just-akash destroy --dseq <DSEQ>` to stop the escrow bleed."
                    )

            # ⛔ A DEGRADED REPORT MUST NOT EXIT 0. A caller checking only the exit
            #   status would otherwise read an incomplete listing as a clean fleet —
            #   the same reasoning `orphan-scan` applies to its own scan, and the same
            #   reason its metric is ABSENT rather than 0 when degraded.
            # ⚠ The findings above are still printed: an unconfirmed listing does not
            #   make the rows it DID return untrue. Degradation dominates the exit
            #   code because exit 0 asserts "these are all of them".
            if is_degraded:
                for reason in degraded_reasons:
                    print(f"\n⚠ DEGRADED: {reason}", file=sys.stderr)
                print(
                    "\n⇒ This is NOT a clean result. Do not treat 'closeable_count: 0' "
                    "from a degraded listing as 'nothing to close'.",
                    file=sys.stderr,
                )
                return 1
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── capacity-probe ─────────────────────────────────
    elif args.command == "capacity-probe":
        import json

        from .api import AkashConsoleAPI
        from .capacity import capacity_probe

        try:
            client = AkashConsoleAPI(_require_api_key())
            use_json = args.json or not sys.stdout.isatty()
            res = capacity_probe(
                client,
                args.gpu_count,
                args.gpu_model,
                wait_s=args.wait,
                provider=args.provider,
            )
            shape = f"{res['gpu_count']}×{res['gpu_model']}"
            if use_json:
                print(json.dumps(res, indent=2))
            elif res["placeable"]:
                print(f"PLACEABLE: {shape} — {len(res['bidders'])} provider(s) bid:")
                for b in res["bidders"]:
                    print(f"  {b['provider']}  @ {b['price_amount']} {b['price_denom']}/block")
                print(f"(probe order {res['dseq']} closed; no lease created)")
            else:
                print(
                    f"NO_BID: {shape} won't place right now "
                    f"(no provider bid in {res['waited_s']}s)."
                )
                print(f"(probe order {res['dseq']} closed; no lease created)")
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── export-metrics ─────────────────────────────────
    elif args.command == "export-metrics":
        from .prometheus_exporter import run as export_metrics

        sys.exit(
            export_metrics(
                args.jsonl,
                output=args.output,
                with_credit=args.with_credit,
                benchmark_path=args.benchmark,
                credit_json=args.credit_json,
            )
        )

    # ── status ─────────────────────────────────────────
    elif args.command == "status":
        from .api import (
            _extract_forwarded_ports,
            _extract_lease_provider,
            _extract_ssh_info,
            _get_tag,
            _json_output,
        )

        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            use_json = args.json or not sys.stdout.isatty()

            deployment = client.get_deployment(dseq)
            dep = deployment.get("deployment", deployment)
            if not isinstance(dep, dict):
                dep = deployment
            state = dep.get("state", "unknown") if isinstance(dep, dict) else "unknown"
            ssh = _extract_ssh_info(deployment)

            if use_json:
                canopy_status = (
                    "ready"
                    if state == "active"
                    else "down"
                    if state in ("closed", "failed")
                    else "unknown"
                )
                result = {
                    "dseq": dseq,
                    "status": canopy_status,
                    "state": state,
                    "provider": _extract_lease_provider(deployment),
                }
                if ssh:
                    result["endpoint"] = f"ssh -p {ssh['port']} root@{ssh['host']}"
                    result["ssh_host"] = ssh["host"]
                    result["ssh_port"] = ssh["port"]
                forwarded = _extract_forwarded_ports(deployment)
                if forwarded:
                    result["endpoints"] = forwarded
                print(_json_output(result))
            else:
                tag = _get_tag(dseq)
                header = f"Deployment {dseq}" + (f"  ({tag})" if tag else "")
                print(f"{header}:")
                print(f"  State:    {state}")
                print(f"  Provider: {_extract_lease_provider(deployment) or 'no lease'}")
                if ssh:
                    print(f"  SSH:      ssh -p {ssh['port']} root@{ssh['host']}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── logs ───────────────────────────────────────────
    elif args.command == "logs":
        try:
            if args.tail < 0:
                print("Error: --tail must be >= 0.", file=sys.stderr)
                sys.exit(1)
            if args.duration is not None and (
                not math.isfinite(args.duration) or args.duration <= 0
            ):
                print("Error: --duration must be a finite number > 0.", file=sys.stderr)
                sys.exit(1)
            client, dseq = _resolve_deployment_client(args.dseq)
            transport = _make_lease_shell(client, dseq)
            try:
                transport.stream_logs(
                    follow=args.follow,
                    tail=args.tail,
                    service=args.service,
                    duration=args.duration,
                )
            except KeyboardInterrupt:
                print()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── events ─────────────────────────────────────────
    elif args.command == "events":
        try:
            if args.duration is not None and (
                not math.isfinite(args.duration) or args.duration <= 0
            ):
                print("Error: --duration must be a finite number > 0.", file=sys.stderr)
                sys.exit(1)
            client, dseq = _resolve_deployment_client(args.dseq)
            transport = _make_lease_shell(client, dseq)
            try:
                transport.stream_events(duration=args.duration)
            except KeyboardInterrupt:
                print()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── add-funds ──────────────────────────────────────
    elif args.command == "add-funds":
        from .api import _confirm, _get_tag

        try:
            if not math.isfinite(args.deposit):
                print("Error: deposit must be a finite number.", file=sys.stderr)
                sys.exit(1)
            if args.deposit < 0.5:
                print("Error: minimum deposit is 0.5 USD.", file=sys.stderr)
                sys.exit(1)
            client, dseq = _resolve_deployment_client(args.dseq)
            tag = _get_tag(dseq)
            label = f"{dseq} ({tag})" if tag else dseq
            if _confirm(f"Add {args.deposit} USD to deployment {label}? (y/N) ", yes=args.yes):
                client.deposit_deployment(dseq, args.deposit)
                print(f"Added {args.deposit} USD to deployment {label}.")
            else:
                print("Cancelled.")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── auto-topup ─────────────────────────────────────
    elif args.command == "auto-topup":
        from .api import _get_tag

        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            tag = _get_tag(dseq)
            label = f"{dseq} ({tag})" if tag else dseq
            if args.on or args.off:
                enabled = bool(args.on)
                # set_auto_top_up reads the setting back and raises if it did not
                # take, so reaching this line means the state was CONFIRMED, not asked
                # for. Saying "verified" is the difference between reporting an outcome
                # and reporting an intention -- this message used to be the latter while
                # reading like the former.
                client.set_auto_top_up(dseq, enabled)
                print(
                    f"Auto top-up {'enabled' if enabled else 'disabled'} "
                    f"for deployment {label} (verified by read-back)."
                )
            else:
                settings = client.get_deployment_settings(dseq)
                if not settings:
                    print(f"Deployment {label}: auto top-up not configured (off).")
                else:
                    # Only a real boolean True means enabled; a non-bool value
                    # (e.g. the string "false") must not read as truthy "on".
                    enabled = settings.get("autoTopUpEnabled") is True
                    print(f"Deployment {label}: auto top-up {'on' if enabled else 'off'}")
                    for key in ("estimatedTopUpAmount", "topUpFrequencyMs"):
                        if key in settings:
                            print(f"  {key}: {settings[key]}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── destroy ────────────────────────────────────────
    elif args.command == "destroy":
        from .api import (
            _confirm,
            _get_tag,
            _load_tags,
            _save_tags,
        )

        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            tag = _get_tag(dseq)
            label = f"{dseq} ({tag})" if tag else dseq
            if _confirm(f"Destroy deployment {label}? (y/N) ", yes=args.yes):
                client.close_deployment(dseq)
                tags = _load_tags()
                tags.pop(dseq, None)
                _save_tags(tags)
                print(f"Deployment {label} destroyed.")
            else:
                print("Cancelled.")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── destroy-all ────────────────────────────────────
    elif args.command == "destroy-all":
        from .api import (
            AkashConsoleAPI,
            _confirm,
            _extract_dseq,
            _load_tags,
            _save_tags,
            format_deployments_table,
        )

        try:
            client = AkashConsoleAPI(_require_api_key())
            deployments = client.list_deployments()
            if not deployments:
                print("No deployments to destroy.")
            else:
                print(format_deployments_table(deployments))
                if _confirm("\nDestroy all? (y/N) ", yes=args.yes):
                    client.close_all_deployments()
                    tags = _load_tags()
                    for d in deployments:
                        dseq_val = _extract_dseq(d)
                        if dseq_val:
                            tags.pop(dseq_val, None)
                    _save_tags(tags)
                    print("All deployments destroyed.")
                else:
                    print("Cancelled.")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── tag ────────────────────────────────────────────
    elif args.command == "tag":
        from .api import _load_tags, _save_tags

        tags = _load_tags()
        tags[args.dseq] = args.name
        _save_tags(tags)
        print(f"Tagged {args.dseq} as '{args.name}'")

    # ── test ───────────────────────────────────────────
    elif args.command == "test":
        from .test_lifecycle import main as test_main

        test_main()

    # ── lease-remaining ────────────────────────────────
    elif args.command == "lease-remaining":
        from .api import _json_output, compute_lease_runway

        try:
            client, dseq = _resolve_deployment_client(args.dseq)
            block_time = (
                args.block_time
                if args.block_time is not None
                else float(os.environ.get("AKASH_BLOCK_TIME_S", "6.0"))
            )
            use_json = args.json or not sys.stdout.isatty()
            runway = compute_lease_runway(client, dseq, block_time_s=block_time)
            if use_json:
                print(_json_output(runway))
            else:
                esc = runway["escrow"]
                burn = runway["burn_rate"]
                usd = f" (≈ ${esc['usd_estimate']})" if esc["usd_estimate"] else ""
                print(f"Deployment {dseq}:")
                print(f"  Provider:       {runway['provider']}")
                print(f"  Escrow:         {esc['display']}{usd}")
                print(
                    f"  Burn rate:      {burn['per_block']} {burn['denom']}/block "
                    f"→ {burn['per_hour']:,.0f} {burn['denom']}/h"
                )
                print(
                    f"  Time remaining: {runway['time_remaining_display']} "
                    f"({runway['time_remaining_hours']:.1f}h)"
                )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── validate-sdl ───────────────────────────────────
    elif args.command == "validate-sdl":
        from pathlib import Path

        from .sdl_validate import SDLValidationError, validate_sdl

        sdl_path = Path(args.sdl)
        if not sdl_path.is_file():
            print(f"Error: SDL file not found: {sdl_path}", file=sys.stderr)
            sys.exit(1)
        try:
            sdl_text = sdl_path.read_text()
        except OSError as e:
            print(f"Error: cannot read {sdl_path}: {e}", file=sys.stderr)
            sys.exit(1)
        try:
            validate_sdl(sdl_text)
        except SDLValidationError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"OK: {sdl_path}")


if __name__ == "__main__":
    main()
