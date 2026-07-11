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
    parser = argparse.ArgumentParser(
        prog="just-akash",
        description="CLI for deploying on Akash Network via the Console API",
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
        help="Phase 1 (preferred-only) window seconds (default: 60)",
    )
    deploy_p.add_argument(
        "--bid-wait-retry",
        type=int,
        default=120,
        help="Phase 2 (preferred-grace) window seconds (default: 120)",
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
        "--backup-provider",
        action="append",
        dest="backup_providers",
        default=None,
        help="Backup provider address (repeatable; overrides AKASH_PROVIDERS_BACKUP)",
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
        help="Service (container) to target. Required when the deployment has several services, or when the lease has not yet reported its service status -- inference reads lease.status.services, which the Console API populates lazily, so it can be empty even after a container is demonstrably up.",
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
        help="Service (container) to target. Required when the deployment has several services, or when the lease has not yet reported its service status -- inference reads lease.status.services, which the Console API populates lazily, so it can be empty even after a container is demonstrably up.",
    )
    exec_p.add_argument("remote_cmd", help="Command to execute remotely")

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

    # ── events ─────────────────────────────────────────
    events_p = subparsers.add_parser(
        "events", help="Stream Kubernetes events for a deployment (debug startup)"
    )
    events_p.add_argument("--dseq", default="")

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
        from .deploy import deploy

        try:
            deploy(
                sdl_path=args.sdl,
                gpu=args.gpu,
                image=args.image,
                bid_wait=args.bid_wait,
                bid_wait_retry=args.bid_wait_retry,
                env_vars=args.deploy_env_vars,
                preferred_providers=args.preferred_providers,
                backup_providers=args.backup_providers,
                deposit=args.deposit,
            )
            sys.exit(0)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── update ─────────────────────────────────────────
    elif args.command == "update":
        from .api import AkashConsoleAPI
        from .deploy import update

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
            update(
                dseq=dseq,
                sdl_path=args.sdl,
                image=args.image,
                env_vars=args.update_env_vars,
            )
            sys.exit(0)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── connect ────────────────────────────────────────
    elif args.command == "connect":
        from .api import AkashConsoleAPI

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
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
        from .api import AkashConsoleAPI

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
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

    # ── inject ─────────────────────────────────────────
    elif args.command == "inject":
        from .api import AkashConsoleAPI

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)

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

    # ── status ─────────────────────────────────────────
    elif args.command == "status":
        from .api import (
            AkashConsoleAPI,
            _extract_forwarded_ports,
            _extract_lease_provider,
            _extract_ssh_info,
            _get_tag,
            _json_output,
        )

        try:
            client = AkashConsoleAPI(_require_api_key())
            use_json = args.json or not sys.stdout.isatty()
            dseq = _resolve_deployment(client, args.dseq)

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
        from .api import AkashConsoleAPI

        try:
            if args.tail < 0:
                print("Error: --tail must be >= 0.", file=sys.stderr)
                sys.exit(1)
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
            transport = _make_lease_shell(client, dseq)
            try:
                transport.stream_logs(follow=args.follow, tail=args.tail, service=args.service)
            except KeyboardInterrupt:
                print()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── events ─────────────────────────────────────────
    elif args.command == "events":
        from .api import AkashConsoleAPI

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
            transport = _make_lease_shell(client, dseq)
            try:
                transport.stream_events()
            except KeyboardInterrupt:
                print()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── add-funds ──────────────────────────────────────
    elif args.command == "add-funds":
        from .api import AkashConsoleAPI, _confirm, _get_tag

        try:
            if not math.isfinite(args.deposit):
                print("Error: deposit must be a finite number.", file=sys.stderr)
                sys.exit(1)
            if args.deposit < 0.5:
                print("Error: minimum deposit is 0.5 USD.", file=sys.stderr)
                sys.exit(1)
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
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
        from .api import AkashConsoleAPI, _get_tag

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
            tag = _get_tag(dseq)
            label = f"{dseq} ({tag})" if tag else dseq
            if args.on or args.off:
                enabled = bool(args.on)
                client.set_auto_top_up(dseq, enabled)
                print(
                    f"Auto top-up {'enabled' if enabled else 'disabled'} for deployment {label}."
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
            AkashConsoleAPI,
            _confirm,
            _get_tag,
            _load_tags,
            _save_tags,
        )

        try:
            client = AkashConsoleAPI(_require_api_key())
            dseq = _resolve_deployment(client, args.dseq)
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
