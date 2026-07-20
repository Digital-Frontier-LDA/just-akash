#!/usr/bin/env python3
"""
Akash Console API client with CLI dispatch.

Provides:
- AkashConsoleAPI class for interacting with Akash Console API
- CLI subcommands: list, status, close, close-all, tag
- Shared helpers: _confirm, _json_output
"""

import base64
import contextlib
import json
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("akash.api")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TAGS_FILE = Path(__file__).resolve().parent.parent / ".tags.json"


def _load_tags() -> dict[str, str]:
    if TAGS_FILE.exists():
        try:
            data = json.loads(TAGS_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_tags(tags: dict[str, str]):
    content = json.dumps(tags, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=TAGS_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, str(TAGS_FILE))
    except BaseException:
        os.unlink(tmp_path) if os.path.exists(tmp_path) else None
        raise


def _get_tag(dseq: str) -> str:
    return _load_tags().get(str(dseq), "")


def _resolve_dseq(identifier: str) -> str:
    if not identifier:
        return ""
    tags = _load_tags()
    for dseq, tag in tags.items():
        if tag == identifier:
            return dseq
    if identifier.isdigit():
        return identifier
    print(f"Error: No deployment found with tag '{identifier}'")
    print(f"Active tags: {', '.join(tags.values()) or 'none'}")
    sys.exit(1)


def _confirm(prompt: str, yes: bool = False) -> bool:
    if yes:
        return True
    try:
        return input(prompt).strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def _json_output(data: dict[str, Any] | list[Any]) -> str:
    return json.dumps(data, indent=2)


def _unwrap_data(response: Any) -> dict[str, Any]:
    """Extract the dict payload from a Console API response.

    Accepts both the ``{"data": {...}}`` envelope and a bare dict body. Returns
    ``{}`` for anything without a usable dict payload — including the
    ``{"data": null}`` shape some endpoints use for "no record", which must NOT
    leak back to callers as a truthy wrapper.
    """
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    return data if isinstance(data, dict) else {}


class AkashConsoleAPI:
    """Client for Akash Console API (https://console-api.akash.network)"""

    def __init__(self, api_key: str, base_url: str | None = None):
        self.base_url = base_url or os.environ.get(
            "AKASH_CONSOLE_URL", "https://console-api.akash.network"
        )
        self.api_key = api_key
        self.headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "akash-just-targets/1.0",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{endpoint}"

        logger.debug(
            f"[{_ts()}] API {method} {endpoint} data={json.dumps(data) if data else 'none'}"
        )

        request_body = json.dumps(data).encode("utf-8") if data else None

        # S310: the URL is built from base_url, which defaults to the https
        # Console API and is operator-set (env var), not external/attacker input.
        req = urllib.request.Request(  # noqa: S310
            url,
            data=request_body,
            headers=self.headers,
            method=method,
        )

        try:
            t0 = datetime.now(timezone.utc)
            with urllib.request.urlopen(req) as response:  # noqa: S310
                response_data = response.read().decode("utf-8")
                elapsed_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
                if response_data:
                    try:
                        result = json.loads(response_data)
                        if not isinstance(result, (dict, list)):
                            result = {"raw": result}
                    except json.JSONDecodeError:
                        result = {"raw": response_data}
                else:
                    result = {}
                logger.debug(
                    f"[{_ts()}] API {method} {endpoint} -> "
                    f"{response.status} ({elapsed_ms}ms) keys="
                    f"{list(result.keys()) if isinstance(result, dict) else type(result).__name__}"
                )
                return result
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            elapsed_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            logger.error(
                f"[{_ts()}] API {method} {endpoint} -> HTTP {e.code} ({elapsed_ms}ms) "
                f"body={error_body[:500]}"
            )
            try:
                error_json = json.loads(error_body)
                error_msg = (
                    error_json.get("message", error_body)
                    if isinstance(error_json, dict)
                    else error_body
                )
            except json.JSONDecodeError:
                error_msg = error_body
            raise RuntimeError(f"API Error ({e.code}): {error_msg}") from e
        except urllib.error.URLError as e:
            logger.error(f"[{_ts()}] API {method} {endpoint} -> URLError: {e}")
            raise RuntimeError(f"Connection error: {e}") from e

    def list_deployments(self, active_only: bool = True) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/deployments")
        if not isinstance(response, dict):
            return []
        data = response.get("data", response)
        if isinstance(data, list):
            deployments = [d for d in data if isinstance(d, dict)]
        elif isinstance(data, dict):
            raw = data.get("deployments", [])
            deployments = raw if isinstance(raw, list) else []
        else:
            deployments = []
        if active_only:
            result = []
            for d in deployments:
                if not isinstance(d, dict):
                    continue
                dep_field = d.get("deployment", {})
                if not isinstance(dep_field, dict):
                    continue
                if dep_field.get("state") == "active":
                    result.append(d)
            deployments = result
        return deployments

    def get_deployment(self, dseq: str) -> dict[str, Any]:
        response = self._request("GET", f"/v1/deployments/{dseq}")
        if not isinstance(response, dict):
            return {}
        data = response.get("data", response)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            first = data[0] if data else {}
            return first if isinstance(first, dict) else response
        return response

    def create_deployment(self, sdl_content: str, deposit: float = 5.0) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/deployments",
            {"data": {"sdl": sdl_content, "deposit": deposit}},
        )
        if not isinstance(response, dict):
            return response if isinstance(response, dict) else {}
        data = response.get("data", response)
        return data if isinstance(data, dict) else response

    def update_deployment(self, dseq: str, sdl_content: str) -> dict[str, Any]:
        """Update an active deployment in place with a revised SDL.

        PUTs to /v1/deployments/{dseq}. The on-chain deployment keeps its DSEQ
        and existing lease — no re-bid or new lease is required. Returns the
        full deployment object (same shape as get_deployment).
        """
        response = self._request(
            "PUT",
            f"/v1/deployments/{dseq}",
            {"data": {"sdl": sdl_content}},
        )
        return _unwrap_data(response)

    def deposit_deployment(self, dseq: str, deposit: float) -> dict[str, Any]:
        """Add funds to an existing deployment's escrow.

        POSTs to /v1/deposit-deployment. ``deposit`` is in USD (minimum 0.5 per
        the Console API). Returns the full deployment object after top-up.
        """
        response = self._request(
            "POST",
            "/v1/deposit-deployment",
            {"data": {"dseq": str(dseq), "deposit": deposit}},
        )
        return _unwrap_data(response)

    def get_deployment_settings(self, dseq: str) -> dict[str, Any]:
        """Fetch auto top-up settings for a deployment.

        GETs /v2/deployment-settings/{dseq}. Returns the settings object, or an
        empty dict if no settings have been created yet (some deployments 404
        until settings are first POSTed — callers should treat {} as "unset").
        """
        try:
            response = self._request("GET", f"/v2/deployment-settings/{dseq}")
        except RuntimeError as e:
            # No settings yet is reported as HTTP 404. Match the status code
            # precisely (_request formats errors as "API Error (404): ...");
            # a substring search would misclassify a 400/500 whose body merely
            # contains "404" (e.g. dseq 40400) or the phrase "not found".
            if str(e).startswith("API Error (404)"):
                return {}
            raise
        return _unwrap_data(response)

    def create_deployment_settings(self, dseq: str, auto_top_up_enabled: bool) -> dict[str, Any]:
        """Create deployment settings (first-time auto top-up config).

        POSTs to /v2/deployment-settings. Use update_deployment_settings to
        change settings that already exist.
        """
        response = self._request(
            "POST",
            "/v2/deployment-settings",
            {"data": {"dseq": str(dseq), "autoTopUpEnabled": auto_top_up_enabled}},
        )
        return _unwrap_data(response)

    def update_deployment_settings(self, dseq: str, auto_top_up_enabled: bool) -> dict[str, Any]:
        """Update existing deployment settings.

        PATCHes /v2/deployment-settings/{dseq}.
        """
        response = self._request(
            "PATCH",
            f"/v2/deployment-settings/{dseq}",
            {"data": {"autoTopUpEnabled": auto_top_up_enabled}},
        )
        return _unwrap_data(response)

    def set_auto_top_up(self, dseq: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable auto top-up, upserting settings as needed.

        Reads current settings: PATCHes if they already exist, otherwise POSTs
        to create them. Returns the resulting settings object.
        """
        existing = self.get_deployment_settings(dseq)
        if existing:
            return self.update_deployment_settings(dseq, enabled)
        return self.create_deployment_settings(dseq, enabled)

    def close_deployment(self, dseq: str) -> dict[str, Any]:
        response = self._request("DELETE", f"/v1/deployments/{dseq}")
        if not isinstance(response, dict):
            return {}
        data = response.get("data", response)
        return data if isinstance(data, dict) else response

    def close_all_deployments(self) -> dict[str, Any]:
        deployments = self.list_deployments()
        results = []
        for deployment in deployments:
            dep_dseq = _extract_dseq(deployment)
            if not dep_dseq:
                continue
            try:
                result = self.close_deployment(dep_dseq)
                results.append(result)
            except Exception as e:
                print(f"Warning: Failed to close deployment {dep_dseq}: {e}")
        return {"closed": results}

    def get_bids(self, dseq: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/v1/bids?dseq={dseq}")
        if not isinstance(response, dict):
            return []
        data = response.get("data", response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            bids = data.get("bids")
            return bids if isinstance(bids, list) else []
        return []

    def get_provider(self, owner: str) -> dict[str, Any] | None:
        try:
            response = self._request("GET", "/v1/providers")
            if isinstance(response, list):
                providers = response
            elif isinstance(response, dict):
                data = response.get("data", response)
                if isinstance(data, list):
                    providers = data
                elif isinstance(data, dict):
                    raw = data.get("providers", [])
                    providers = raw if isinstance(raw, list) else []
                else:
                    providers = []
            else:
                providers = []
            for p in providers:
                if isinstance(p, dict) and p.get("owner") == owner:
                    return p
        except RuntimeError:
            pass
        return None

    def create_lease(
        self, dseq: str, provider: str, manifest: str, gseq: int = 1, oseq: int = 1
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/v1/leases",
            {
                "manifest": manifest,
                "leases": [
                    {
                        "dseq": str(dseq),
                        "gseq": gseq,
                        "oseq": oseq,
                        "provider": provider,
                    }
                ],
            },
        )
        if not isinstance(response, dict):
            return {}
        return response

    def create_jwt(self, dseq: str, ttl: int = 3600, scope: list[str] | None = None) -> str:
        """Request a short-lived JWT for provider access (shell, logs, events…).

        POSTs to /v1/create-jwt-token with the existing api_key.
        Returns the JWT string. Raises RuntimeError on HTTP error.

        Args:
            dseq: Deployment sequence number. NOTE: currently unused in the request
                  body — a top-level ``scoped`` grant is not deployment-scoped (it
                  applies across the owner's leases), and AEP-64 forbids naming a
                  deployment on a scoped grant. Kept in the signature for call-site
                  compatibility and possible future granular use; do not assume the
                  minted token is bound to this dseq.
            ttl:  Requested TTL in seconds (server default is 30s; request 3600 and
                  fall back to reconnect if server caps it — see LSHL-03 in Phase 7).
            scope: Permission scopes to request (e.g. ["shell"], ["logs"],
                  ["events"]). Defaults to ["shell"]. Valid values: send-manifest,
                  get-manifest, logs, shell, events, status, restart,
                  hostname-migrate, ip-migrate.
        """
        # `is None`, not `or`: an omitted scope defaults to ["shell"], but an
        # explicitly empty list is passed through unchanged rather than silently
        # widened to shell. (The API rejects an empty scope anyway -- AEP-64 requires
        # at least one -- so this surfaces the caller's mistake instead of granting a
        # permission they did not ask for.)
        if scope is None:
            scope = ["shell"]
        # "scoped", not "full". The Console API accepts exactly two shapes at
        # /leases -- {"access": "scoped", "scope": [...]} or {"access": "granular",
        # "permissions": [...]} -- and rejects everything else. The old body paired
        # access "full" with a scope and got a 400 on every call:
        #
        #   Additional property "scope" is not allowed at "/leases"..
        #   "access" at "/leases" must be scoped.. "access" at "/leases" must be granular.
        #
        # so this fallback (taken when a lease reports no provider address, hence no
        # provider to scope the grant to) could never have minted a token. Per AEP-64,
        # top-level "scoped" grants the scope across the owner's leases, which is the
        # right grant to make when we have no provider to narrow it to.
        response = self._request(
            "POST",
            "/v1/create-jwt-token",
            {"data": {"ttl": ttl, "leases": {"access": "scoped", "scope": scope}}},
        )
        # Response shape: { "data": { "token": "<JWT>" } }
        if not isinstance(response, dict):
            raise RuntimeError(f"Unexpected JWT response type: {type(response)}")
        data = response.get("data", response)
        if isinstance(data, dict) and "token" in data:
            token = data["token"]
            if isinstance(token, str) and token:
                return token
        raise RuntimeError(f"JWT token not found in response: {response}")

    def account_address(self) -> str:
        """The on-chain account address behind this API key.

        The Console API has no "whoami" endpoint, but every JWT it mints carries the
        account as the ``iss`` (issuer) claim, so we read it from a short-lived token.
        We decode only that public claim from the unauthenticated base64 payload (the
        signature is never verified, and the token is never used), so this is a cheap,
        side-effect-free identity probe.
        """
        token = self.create_jwt(dseq="0", ttl=300)
        parts = token.split(".")
        if len(parts) < 2:
            raise RuntimeError(f"malformed JWT (no payload segment): {token[:24]}...")
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)  # restore base64url padding
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        except ValueError as e:
            # ValueError covers every decode failure: binascii.Error (bad base64),
            # UnicodeDecodeError, and json.JSONDecodeError — all normalized to one
            # RuntimeError so a malformed token never escapes as a different type.
            raise RuntimeError(f"could not decode JWT payload: {e}") from e
        addr = claims.get("iss")
        if not isinstance(addr, str) or not addr.startswith("akash1"):
            raise RuntimeError(f"JWT payload has no akash issuer claim: {claims!r}")
        return addr

    def create_jwt_with_provider(
        self, dseq: str, provider: str, ttl: int = 3600, scope: list[str] | None = None
    ) -> str:
        """Request a short-lived JWT scoped to a specific provider.

        Uses granular access with scoped permissions. ``scope`` selects which
        provider operations the token authorizes (defaults to ["shell"]; pass
        ["logs"] or ["events"] for streaming). The token is scoped to ``provider``,
        NOT to a deployment: ``dseq`` is currently unused in the request body (a
        scoped permission cannot name a deployment under AEP-64) and is kept only
        for call-site compatibility — do not assume the token is bound to this dseq.
        """
        # `is None`, not `or`: an omitted scope defaults to ["shell"], but an
        # explicitly empty list is passed through unchanged rather than silently
        # widened to shell. (The API rejects an empty scope anyway -- AEP-64 requires
        # at least one -- so this surfaces the caller's mistake instead of granting a
        # permission they did not ask for.)
        if scope is None:
            scope = ["shell"]
        response = self._request(
            "POST",
            "/v1/create-jwt-token",
            {
                "data": {
                    "ttl": ttl,
                    "leases": {
                        "access": "granular",
                        "permissions": [
                            {
                                "provider": provider,
                                "access": "scoped",
                                "scope": scope,
                            }
                        ],
                    },
                }
            },
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"Unexpected JWT response type: {type(response)}")
        data = response.get("data", response)
        if isinstance(data, dict) and "token" in data:
            token = data["token"]
            if isinstance(token, str) and token:
                return token
        raise RuntimeError(f"JWT token not found in response: {response}")


def _extract_dseq(deployment: dict[str, Any]) -> str | None:
    if not isinstance(deployment, dict):
        return None
    if "dseq" in deployment:
        val = deployment["dseq"]
        return str(val) if val is not None else None
    dep = deployment.get("deployment", {})
    if not isinstance(dep, dict):
        return None
    dep_id = dep.get("id", {})
    if not isinstance(dep_id, dict):
        return None
    if "dseq" in dep_id:
        val = dep_id["dseq"]
        return str(val) if val is not None else None
    return None


def _extract_provider(bid: dict[str, Any]) -> str | None:
    if not isinstance(bid, dict):
        return None
    nested = bid.get("bid", {})
    nested_id = nested.get("id", {}) if isinstance(nested, dict) else {}
    bid_id = bid.get("id", nested_id)
    if isinstance(bid_id, dict) and "provider" in bid_id:
        return bid_id["provider"]
    return bid.get("provider")


def _extract_bid_price(bid: dict[str, Any]) -> tuple:
    # Display-only fallback denom for malformed bids that omit `denom`. The
    # real denom comes from the bid response when present. Default to BME-era
    # `uact`; legacy `uakt` is no longer canonical.
    if not isinstance(bid, dict):
        return (float("inf"), "uact")
    nested = bid.get("bid", {})
    nested_price = nested.get("price", {}) if isinstance(nested, dict) else {}
    price = bid.get("price", nested_price)
    if isinstance(price, dict):
        raw_amount = price.get("amount", float("inf"))
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            amount = float("inf")
        denom = price.get("denom", "uact")
        return (amount, denom)
    try:
        return (float(price) if price else float("inf"), "uact")
    except (TypeError, ValueError):
        return (float("inf"), "uact")


def _find_ssh_key(explicit_key: str = "") -> str | None:
    if explicit_key:
        return explicit_key if os.path.exists(explicit_key) else None
    for candidate in [
        os.path.expanduser(f"~/.ssh/id_ed25519_akash_node{i}") for i in range(1, 4)
    ] + [
        os.path.expanduser("~/.ssh/id_ed25519"),
        os.path.expanduser("~/.ssh/id_rsa"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return None


def _build_ssh_cmd(ssh_info: dict[str, Any], key_path: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-i",
        key_path,
        "-p",
        str(ssh_info["port"]),
        f"root@{ssh_info['host']}",
    ]


def _extract_ssh_info(deployment: dict[str, Any]) -> dict[str, Any] | None:
    leases = deployment.get("leases")
    for lease in leases if isinstance(leases, list) else []:
        if not isinstance(lease, dict):
            continue
        status = lease.get("status") or {}
        if not isinstance(status, dict):
            continue
        fwd_ports = status.get("forwarded_ports") or {}
        if not isinstance(fwd_ports, dict):
            continue
        for svc_name, ports in fwd_ports.items():
            if not isinstance(ports, list):
                continue
            for p in ports:
                if not isinstance(p, dict):
                    continue
                if p.get("port") == 22:
                    host = p.get("host")
                    external_port = p.get("externalPort")
                    if host is not None and external_port is not None:
                        return {
                            "host": host,
                            "port": external_port,
                            "service": svc_name,
                        }
    return None


def _extract_forwarded_ports(deployment: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every forwarded port a lease exposes.

    Each entry is ``{"internal_port", "host", "port", "service"}`` where ``port``
    is the provider-assigned external port. Used to surface non-SSH endpoints
    (e.g. an LCD/REST server on internal port 1317) that ``_extract_ssh_info``
    deliberately ignores.
    """
    endpoints: list[dict[str, Any]] = []
    leases = deployment.get("leases")
    for lease in leases if isinstance(leases, list) else []:
        if not isinstance(lease, dict):
            continue
        status = lease.get("status") or {}
        if not isinstance(status, dict):
            continue
        fwd_ports = status.get("forwarded_ports") or {}
        if not isinstance(fwd_ports, dict):
            continue
        for svc_name, ports in fwd_ports.items():
            if not isinstance(ports, list):
                continue
            for p in ports:
                if not isinstance(p, dict):
                    continue
                host = p.get("host")
                external_port = p.get("externalPort")
                internal_port = p.get("port")
                if host is not None and external_port is not None and internal_port is not None:
                    endpoints.append(
                        {
                            "internal_port": internal_port,
                            "host": host,
                            "port": external_port,
                            "service": svc_name,
                        }
                    )
    return endpoints


def _extract_lease_provider(deployment: dict[str, Any]) -> str | None:
    leases = deployment.get("leases")
    for lease in leases if isinstance(leases, list) else []:
        if not isinstance(lease, dict):
            continue
        lease_id = lease.get("id", {})
        if isinstance(lease_id, dict) and "provider" in lease_id:
            return lease_id["provider"]
    return None


def format_deployments_table(deployments: list[dict[str, Any]]) -> str:
    if not deployments:
        return "No active deployments."

    tags = _load_tags()
    rows = []
    for d in deployments:
        if not isinstance(d, dict):
            continue
        dseq = _extract_dseq(d) or "?"
        tag = tags.get(dseq, "")
        dep = d.get("deployment", d)
        if not isinstance(dep, dict):
            dep = d
        state = str(dep.get("state", "unknown") if isinstance(dep, dict) else "unknown")
        _provider = _extract_lease_provider(d)
        provider = str(_provider) if _provider is not None else "no lease"
        ssh = _extract_ssh_info(d)
        ssh_col = f"{ssh['host']}:{ssh['port']}" if ssh else "-"
        rows.append((dseq, tag, state, provider[:20], ssh_col))

    headers = ("DSEQ", "Tag", "State", "Provider", "SSH")
    if not rows:
        return "No active deployments."
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False))]
    lines.append("-" * len(lines[0]))
    for row in rows:
        lines.append("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=False)))

    return "\n".join(lines)


def format_deployments_json(deployments: list[dict[str, Any]]) -> str:
    tags = _load_tags()
    rows = []
    for d in deployments:
        if not isinstance(d, dict):
            continue
        dseq = _extract_dseq(d) or "?"
        dep = d.get("deployment", d)
        if not isinstance(dep, dict):
            dep = d
        state = dep.get("state", "unknown") if isinstance(dep, dict) else "unknown"
        provider = _extract_lease_provider(d)
        ssh = _extract_ssh_info(d)
        rows.append(
            {
                "dseq": dseq,
                "tag": tags.get(dseq, ""),
                "state": state,
                "provider": provider or "no lease",
                "ssh": f"{ssh['host']}:{ssh['port']}" if ssh else None,
            }
        )
    return _json_output(rows)


def _interactive_pick(deployments: list[dict[str, Any]], client: "AkashConsoleAPI") -> str:
    import termios
    import tty

    if not deployments:
        raise ValueError("No deployments to pick from")

    if not sys.stdin.isatty():
        if not isinstance(deployments[0], dict):
            raise ValueError("Deployment entry is not a dict")
        dseq = _extract_dseq(deployments[0])
        if not dseq:
            raise RuntimeError("Could not extract dseq from deployment")
        return dseq

    tags = _load_tags()
    items = []
    for d in deployments:
        if not isinstance(d, dict):
            continue
        dseq = _extract_dseq(d) or "?"
        tag = tags.get(dseq, "")
        provider = (_extract_lease_provider(d) or "no lease")[:24]
        ssh = _extract_ssh_info(d)
        ssh_str = f"{ssh['host']}:{ssh['port']}" if ssh else "no SSH"
        label = f"{dseq}  {tag}" if tag else dseq
        items.append((dseq, label, provider, ssh_str))

    if not items:
        raise ValueError("No deployments to pick from")

    selected = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def render():
        out = []
        if render.drawn:
            out.append(f"\033[{len(items) + 1}A")
        out.append(
            "\r\033[K\033[1mSelect deployment:\033[0m  ↑↓ navigate  Enter select  q cancel\r\n"
        )
        for i, (_, label, prov, ssh_str) in enumerate(items):
            marker = "\033[92m▸\033[0m" if i == selected else " "
            highlight = "\033[1m" if i == selected else ""
            reset = "\033[0m"
            out.append(f"\r\033[K  {marker} {highlight}{label}  {prov}  {ssh_str}{reset}\r\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        render.drawn = True

    render.drawn = False

    try:
        tty.setraw(fd)
        render()
        while True:
            ch = sys.stdin.read(1)
            if ch == "\r" or ch == "\n":
                break
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A":
                    selected = (selected - 1) % len(items)
                elif seq == "[B":
                    selected = (selected + 1) % len(items)
                render()
            elif ch == "q" or ch == "\x03":
                sys.stdout.write("\r\nCancelled.\r\n")
                sys.stdout.flush()
                sys.exit(0)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    sys.stdout.write("\r\n")
    dseq = items[selected][0]
    print(f"Selected: {dseq}")
    return dseq


def _format_hours(hours: float) -> str:
    """Human-readable time-remaining from hours."""
    if hours >= 48:
        return f"~{hours / 24:.0f} days"
    if hours >= 1:
        return f"~{hours:.1f} hours"
    return f"~{hours * 60:.0f} minutes"


def compute_lease_runway(
    client: "AkashConsoleAPI", dseq: str, block_time_s: float = 6.0
) -> dict[str, Any]:
    """Estimate how long a deployment's escrow will last at the current burn rate.

    Reads the deployment's escrow balance + the winning bid's per-block price, then
    computes: ``time_remaining = escrow_remaining / (price_per_block × blocks_per_hour)``.

    Requires an active lease (to identify the provider) and a matching bid (for the
    price). Raises ``RuntimeError`` if either is missing, or if the denoms don't match
    (escrow in one denom, price in another — can't compute without conversion).

    Returns a dict with escrow, burn_rate, and time_remaining for both ``--json`` and
    human display. Reuses ``chain.format_amount`` / ``chain.usd_estimate`` for display.
    """
    from .chain import format_amount, usd_estimate

    if block_time_s <= 0 or block_time_s != block_time_s or block_time_s == float("inf"):
        raise RuntimeError(f"block_time_s must be positive and finite; got {block_time_s}")

    deployment = client.get_deployment(dseq)

    # ── escrow remaining ──
    escrow_account = deployment.get("escrow_account") or {}
    # Parenthesized so the precedence is unambiguous (Copilot review).
    escrow_state = (escrow_account.get("state") or {}) if isinstance(escrow_account, dict) else {}
    funds_list = escrow_state.get("funds") or []
    if not isinstance(funds_list, list):
        funds_list = []
    escrow_by_denom: dict[str, int] = {}
    for f in funds_list:
        if not isinstance(f, dict):
            continue
        denom = f.get("denom", "")
        raw = f.get("amount", "0")
        with contextlib.suppress(TypeError, ValueError):
            escrow_by_denom[denom] = escrow_by_denom.get(denom, 0) + int(str(raw).split(".", 1)[0])

    # ── lease provider (to match the winning bid) ──
    lease_provider = _extract_lease_provider(deployment)
    if not lease_provider:
        raise RuntimeError(
            "No active lease on this deployment — cannot determine the burn rate. "
            "The deployment may not have been leased yet."
        )

    # ── winning bid's per-block price ──
    bids = client.get_bids(str(dseq))
    price_per_block: float | None = None
    price_denom: str | None = None
    for b in bids:
        if not isinstance(b, dict):
            continue
        if _extract_provider(b) == lease_provider:
            price_per_block, price_denom = _extract_bid_price(b)
            break
    if price_per_block is None or price_per_block == float("inf"):
        raise RuntimeError(
            f"Could not find the bid price for provider {lease_provider} on deployment {dseq}."
        )

    # ── runway (escrow and price must be the same denom) ──
    if price_denom and price_denom not in escrow_by_denom and escrow_by_denom:
        raise RuntimeError(
            f"Denom mismatch: escrow has {list(escrow_by_denom)} but the bid price "
            f"is in {price_denom}. Cannot compute runway without conversion."
        )
    escrow_amount = escrow_by_denom.get(price_denom or "", 0)
    if price_per_block <= 0:
        raise RuntimeError("Bid price is zero — cannot compute runway.")
    blocks_per_hour = 3600.0 / block_time_s
    burn_per_hour = price_per_block * blocks_per_hour
    time_remaining_h = escrow_amount / burn_per_hour if burn_per_hour > 0 else float("inf")

    return {
        "dseq": str(dseq),
        "provider": lease_provider,
        "escrow": {
            "amount": escrow_amount,
            "denom": price_denom,
            "display": format_amount(price_denom or "", escrow_amount),
            "usd_estimate": usd_estimate(price_denom or "", escrow_amount),
        },
        "burn_rate": {
            "per_block": price_per_block,
            "per_hour": round(burn_per_hour, 1),
            "denom": price_denom,
        },
        "time_remaining_hours": round(time_remaining_h, 1),
        "time_remaining_display": _format_hours(time_remaining_h),
        "block_time_s": block_time_s,
    }


def api_main():
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("Error: AKASH_API_KEY environment variable not set.")
        print(
            "Please set your API key: export AKASH_API_KEY='your-key'"  # pragma: allowlist secret
        )
        sys.exit(1)

    client = AkashConsoleAPI(api_key)

    import argparse

    parser = argparse.ArgumentParser(
        description="Akash Console API CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("list", help="List all active deployments")

    status_p = subparsers.add_parser("status", help="Show deployment details")
    status_p.add_argument("--dseq", default="")

    connect_p = subparsers.add_parser("connect", help="SSH into a running deployment")
    connect_p.add_argument("--dseq", default="")
    connect_p.add_argument("--key", default="")

    close_p = subparsers.add_parser("close", help="Close a deployment")
    close_p.add_argument("--dseq", default="")

    subparsers.add_parser("close-all", help="Close all deployments")

    tag_p = subparsers.add_parser("tag", help="Tag a deployment with a name")
    tag_p.add_argument("--dseq", required=True)
    tag_p.add_argument("--name", required=True)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    use_json = args.json or not sys.stdout.isatty()

    try:
        if args.command == "list":
            deployments = client.list_deployments()
            if use_json:
                print(format_deployments_json(deployments))
            else:
                print(format_deployments_table(deployments))

        elif args.command == "status":
            dseq = _resolve_dseq(args.dseq)
            if not dseq:
                deployments = client.list_deployments()
                if not deployments:
                    if use_json:
                        print(_json_output({"status": "down"}))
                    else:
                        print("No active deployments.")
                    sys.exit(0)
                if len(deployments) == 1:
                    dseq = _extract_dseq(deployments[0])
                    if not dseq:
                        raise RuntimeError("Could not extract dseq from deployment")
                    if not use_json:
                        print(f"Auto-selected deployment {dseq}\n")
                else:
                    dseq = _interactive_pick(deployments, client)
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
                result: dict[str, Any] = {
                    "dseq": dseq,
                    "status": canopy_status,
                    "state": state,
                    "provider": _extract_lease_provider(deployment),
                }
                if ssh:
                    result["endpoint"] = f"ssh -p {ssh['port']} root@{ssh['host']}"
                    result["ssh_host"] = ssh["host"]
                    result["ssh_port"] = ssh["port"]
                print(_json_output(result))
            else:
                tag = _get_tag(dseq)
                header = f"Deployment {dseq}"
                if tag:
                    header += f"  ({tag})"
                print(f"{header}:")
                print(f"  State:    {state}")
                print(f"  Provider: {_extract_lease_provider(deployment) or 'no lease'}")

                if ssh:
                    print(f"  SSH:      ssh -p {ssh['port']} root@{ssh['host']}")

                _leases = deployment.get("leases")
                for lease in _leases if isinstance(_leases, list) else []:
                    if not isinstance(lease, dict):
                        continue
                    lease_status = lease.get("status") or {}
                    if not isinstance(lease_status, dict):
                        continue
                    fwd = lease_status.get("forwarded_ports") or {}
                    if not isinstance(fwd, dict):
                        fwd = {}
                    for svc, ports in fwd.items():
                        if not isinstance(ports, list):
                            continue
                        for p in ports:
                            if not isinstance(p, dict):
                                continue
                            p_port = p.get("port")
                            if p_port is not None and p_port != 22:
                                p_host = p.get("host", "?")
                                p_ext = p.get("externalPort", "?")
                                print(
                                    f"  Port:     {p_host}:{p_ext} "
                                    f"→ {p_port}/{p.get('proto', 'TCP')} ({svc})"
                                )

                    services = lease_status.get("services") or {}
                    if not isinstance(services, dict):
                        services = {}
                    for svc, info in services.items():
                        if not isinstance(info, dict):
                            continue
                        ready = info.get("ready_replicas", 0)
                        total = info.get("total", 0)
                        print(f"  Service:  {svc} ({ready}/{total} ready)")

                escrow_account = deployment.get("escrow_account") or {}
                if isinstance(escrow_account, dict):
                    escrow = escrow_account.get("state") or {}
                    if isinstance(escrow, dict):
                        funds = escrow.get("funds") or []
                        if not isinstance(funds, list):
                            funds = []
                        for f in funds:
                            if not isinstance(f, dict):
                                continue
                            print(f"  Escrow:   {f.get('amount', '?')} {f.get('denom', '?')}")

        elif args.command == "connect":
            dseq = _resolve_dseq(args.dseq)
            if not dseq:
                deployments = client.list_deployments()
                if not deployments:
                    print("No active deployments.")
                    sys.exit(1)
                if len(deployments) == 1:
                    dseq = _extract_dseq(deployments[0])
                    if not dseq:
                        raise RuntimeError("Could not extract dseq from deployment")
                    print(f"Auto-selected deployment {dseq}")
                else:
                    dseq = _interactive_pick(deployments, client)

            if not dseq:
                raise RuntimeError("No deployment selected")
            deployment = client.get_deployment(dseq)
            ssh = _extract_ssh_info(deployment)
            if not ssh:
                print(f"No SSH port (22) found on deployment {dseq}.")
                print("Deploy with SSH SDL: just up")
                sys.exit(1)

            key_path = _find_ssh_key(args.key)
            if not key_path:
                print("No SSH key found. Specify with --key")
                sys.exit(1)

            ssh_cmd = _build_ssh_cmd(ssh, key_path)
            print(f"Connecting to {ssh['host']}:{ssh['port']}...")
            os.execvp("ssh", ssh_cmd)

        elif args.command == "close":
            dseq = _resolve_dseq(args.dseq)
            if not dseq:
                deployments = client.list_deployments()
                if not deployments:
                    print("No active deployments.")
                    sys.exit(0)
                if len(deployments) == 1:
                    dseq = _extract_dseq(deployments[0])
                    if not dseq:
                        raise RuntimeError("Could not extract dseq from deployment")
                    print(f"Auto-selected deployment {dseq}")
                else:
                    dseq = _interactive_pick(deployments, client)

            if not dseq:
                raise RuntimeError("No deployment selected")
            tag = _get_tag(dseq)
            label = f"{dseq} ({tag})" if tag else dseq
            if _confirm(f"Close deployment {label}? (y/N) ", yes=args.yes):
                client.close_deployment(dseq)
                tags = _load_tags()
                tags.pop(dseq, None)
                _save_tags(tags)
                print(f"Deployment {label} closed.")
            else:
                print("Cancelled.")

        elif args.command == "close-all":
            deployments = client.list_deployments()
            if not deployments:
                print("No deployments to close.")
            else:
                print(f"Found {len(deployments)} active deployment(s):")
                print(format_deployments_table(deployments))
                if _confirm("\nClose all? (y/N) ", yes=args.yes):
                    client.close_all_deployments()
                    tags = _load_tags()
                    for d in deployments:
                        dseq = _extract_dseq(d)
                        if dseq:
                            tags.pop(dseq, None)
                    _save_tags(tags)
                    print("All deployments closed.")
                else:
                    print("Cancelled.")

        elif args.command == "tag":
            tags = _load_tags()
            tags[args.dseq] = args.name
            _save_tags(tags)
            print(f"Tagged {args.dseq} as '{args.name}'")

    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
