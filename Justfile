set dotenv-load

# ── Lifecycle ────────────────────────────────────────

# Start a new Akash instance (SSH-enabled, key-auth only)
# Usage: just up [tag]
up tag="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/up-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=up finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=up started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file tag={{tag}}"
    set -x
    uv run just-akash deploy --sdl sdl/cpu-backtest-ssh.yaml --bid-wait 60 --bid-wait-retry 120 | tee /tmp/.akash-last-deploy.log
    dseq=$(sed -n 's/.*DSEQ: \([0-9]*\).*/\1/p' /tmp/.akash-last-deploy.log | head -1)
    if [ -n "{{tag}}" ] && [ -n "$dseq" ]; then
        uv run just-akash tag --dseq "$dseq" --name "{{tag}}"
    fi

# Connect to a running instance via lease-shell (default) or SSH
# Usage: just connect [dseq] [transport=lease-shell|ssh]
connect dseq="" transport="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/connect-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=connect finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=connect started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}}"
    set -x
    args=(uv run just-akash connect)
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    if [ -n "{{transport}}" ]; then args+=(--transport "{{transport}}"); fi
    "${args[@]}"

# Update a running instance in place with a revised SDL (no re-bid, keeps DSEQ).
# Usage: just update SDL [dseq] [image]
#   just update sdl/cpu-backtest-ssh.yaml
#   just update sdl/cpu-backtest-ssh.yaml akash-node ghcr.io/me/app:v2
update sdl dseq="" image="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/update-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=update finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=update started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file sdl={{sdl}} dseq={{dseq}} image={{image}}"
    set -x
    args=(uv run just-akash update --sdl "{{sdl}}")
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    if [ -n "{{image}}" ]; then args+=(--image "{{image}}"); fi
    "${args[@]}"

# Destroy an instance (picks interactively if no DSEQ given)
destroy dseq="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/destroy-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=destroy finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=destroy started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}}"
    set -x
    if [ -n "{{dseq}}" ]; then
        uv run just-akash destroy --dseq={{dseq}}
    else
        uv run just-akash destroy
    fi

# Destroy all instances
destroy-all:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/destroy-all-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=destroy-all finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=destroy-all started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run just-akash destroy-all

# Tag a deployment with a name
# Usage: just tag DSEQ my-backtest
tag dseq name:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/tag-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=tag finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=tag started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}} name={{name}}"
    set -x
    uv run just-akash tag --dseq={{dseq}} --name "{{name}}"

# Inject secrets into a running instance via lease-shell (default) or SSH
# Usage: just inject [dseq] [env-file] [transport=lease-shell|ssh]
#   just inject "" .env.secrets
#   just inject 12345 .env.secrets
inject dseq="" env-file=".env.secrets" transport="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/inject-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=inject finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=inject started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}} env_file={{env-file}}"
    set -x
    args=(uv run just-akash inject --env-file "{{env-file}}")
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    if [ -n "{{transport}}" ]; then args+=(--transport "{{transport}}"); fi
    "${args[@]}"

# Execute a command on a running instance via lease-shell (default) or SSH
# Usage: just exec [dseq] [transport=lease-shell|ssh] "command"
exec dseq="" command="" transport="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/exec-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=exec finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=exec started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}} command={{command}}"
    set -x
    args=(uv run just-akash exec)
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    if [ -n "{{transport}}" ]; then args+=(--transport "{{transport}}"); fi
    args+=("{{command}}")
    "${args[@]}"

# ── Info ─────────────────────────────────────────────

# List active instances
list:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/list-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=list finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=list started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run just-akash list

# Show instance details (picks interactively if no DSEQ given)
status dseq="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/status-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=status finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=status started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}}"
    set -x
    if [ -n "{{dseq}}" ]; then
        uv run just-akash status --dseq={{dseq}}
    else
        uv run just-akash status
    fi

# Stream container logs (picks interactively if no DSEQ given).
# Usage: just logs [dseq] [follow]   (pass any non-empty follow arg to tail -f)
#   just logs akash-node
#   just logs akash-node follow
logs dseq="" follow="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/logs-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=logs finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=logs started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}} follow={{follow}}"
    set -x
    args=(uv run just-akash logs)
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    if [ -n "{{follow}}" ]; then args+=(--follow); fi
    "${args[@]}"

# Stream Kubernetes events for a deployment (debug why it won't start).
# Usage: just events [dseq]
events dseq="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/events-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=events finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=events started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}}"
    set -x
    args=(uv run just-akash events)
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    "${args[@]}"

# ── Escrow / funding ─────────────────────────────────

# Add funds (USD) to a deployment's escrow so it outlives its initial deposit.
# Usage: just add-funds AMOUNT [dseq]   (AMOUNT in USD, minimum 0.5)
#   just add-funds 5 akash-node
add-funds amount dseq="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/add-funds-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=add-funds finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=add-funds started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file amount={{amount}} dseq={{dseq}}"
    set -x
    args=(uv run just-akash add-funds --deposit "{{amount}}")
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    "${args[@]}"

# Show or toggle auto top-up for a deployment.
# Usage: just auto-topup [dseq] [on|off]   (no toggle = show current setting)
#   just auto-topup akash-node on
auto-topup dseq="" toggle="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/auto-topup-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=auto-topup finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=auto-topup started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file dseq={{dseq}} toggle={{toggle}}"
    set -x
    args=(uv run just-akash auto-topup)
    if [ -n "{{dseq}}" ]; then args+=(--dseq "{{dseq}}"); fi
    if [ "{{toggle}}" = "on" ]; then args+=(--on); fi
    if [ "{{toggle}}" = "off" ]; then args+=(--off); fi
    "${args[@]}"

# ── Testing ──────────────────────────────────────────

# Full lifecycle test: up → verify provider → SSH → down → cleanup
test:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/test-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=test finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=test started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run python -m just_akash.test_lifecycle

# Secrets injection E2E: deploy → inject via lease-shell → verify via SSH → cleanup
test-secrets:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/test-secrets-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=test-secrets finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=test-secrets started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run python -m just_akash.test_secrets_e2e

# E2E lease-shell transport test: deploy → exec/inject via lease-shell → verify → cleanup
test-shell:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/test-shell-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=test-shell finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=test-shell started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run python -m just_akash.test_shell_e2e

# Provider capability smoke test: deploy a throwaway probe to each configured
# provider, exercise every provider-facing feature (exec, inject, logs, events,
# SSH transport, connect, HTTP ingress, in-place update), destroy, and print a
# pass/fail matrix. Pass args through, e.g. `just smoke-providers --all`.
smoke-providers *args:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/smoke-providers-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=smoke-providers finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=smoke-providers started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run python -m just_akash.smoke_providers {{args}}

# Aggregate accrued smoke telemetry into per-(provider,feature) latency
# percentiles + success rate. With no FILE, pulls the live `telemetry` branch.
#   just smoke-telemetry-report                 # analyze the accrued CI data
#   just smoke-telemetry-report path.jsonl       # analyze a local file
smoke-telemetry-report file="":
    #!/bin/bash
    set -euo pipefail
    if [ -n "{{file}}" ]; then
        uv run python -m just_akash.analyze_telemetry "{{file}}"
    else
        tmp="$(mktemp)"
        trap 'rm -f "$tmp"' EXIT   # clean up on every exit path, not just success
        if ! git fetch origin telemetry >/dev/null 2>&1; then echo "no telemetry branch yet"; exit 0; fi
        git show origin/telemetry:smoke-latency.jsonl > "$tmp"
        uv run python -m just_akash.analyze_telemetry "$tmp"
    fi

# Render accrued smoke telemetry as Prometheus textfile-collector metrics so
# no-credit/no-bid/lease-down outcomes + deploy-credit burn-down are Grafana-trackable.
# With no FILE, pulls the live `telemetry` branch. OUTPUT writes a .prom atomically;
# with-credit=1 also emits the deploy-credit gauge (needs AKASH_API_KEY).
#   just export-metrics path.jsonl                    # -> stdout
#   just export-metrics path.jsonl smoke.prom          # -> file
#   just export-metrics "" smoke.prom with-credit=1     # accrued CI data + credit gauge
export-metrics file="" output="" with-credit="":
    #!/bin/bash
    set -euo pipefail
    args=()
    if [ -n "{{output}}" ]; then args+=(--output "{{output}}"); fi
    if [ -n "{{with-credit}}" ]; then args+=(--with-credit); fi
    if [ -n "{{file}}" ]; then
        uv run just-akash export-metrics "{{file}}" "${args[@]}"
    else
        tmp="$(mktemp)"
        trap 'rm -f "$tmp"' EXIT   # clean up on every exit path
        if ! git fetch origin telemetry >/dev/null 2>&1; then echo "no telemetry branch yet"; exit 0; fi
        git show origin/telemetry:smoke-latency.jsonl > "$tmp"
        uv run just-akash export-metrics "$tmp" "${args[@]}"
    fi

# ── Lint & Quality ───────────────────────────────────

# Run ruff lint + format check
lint:
    uv run ruff check . && uv run ruff format --check .

# Run pyright type check
typecheck:
    uv run pyright

# Run ruff format (auto-fix)
fmt:
    uv run ruff format .

# Run ruff check (auto-fix)
check:
    uv run ruff check --fix .

# ── Secrets ──────────────────────────────────────────

# Scan for secrets with gitleaks
secrets:
    gitleaks detect --no-banner -v

# ── Security (SAST + dependency CVEs) ────────────────

# Static security scan with Semgrep (excludes 2 CLI-inherent rules; see SECURITY.md)
semgrep:
    #!/bin/bash
    set -euo pipefail
    uvx semgrep scan \
      --config p/python --config p/security-audit \
      --exclude-rule python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args \
      --exclude-rule python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected \
      --error --metrics off just_akash/

# Dependency CVE audit (pip-audit over the synced environment).
audit:
    uv run --with pip-audit pip-audit

# ── Advanced ─────────────────────────────────────────

# Deploy with custom SDL (e.g. no SSH, different image)
deploy sdl="sdl/cpu-backtest-ssh.yaml" image="":
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/deploy-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=deploy finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=deploy started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file sdl={{sdl}} image={{image}}"
    set -x
    args=(uv run just-akash deploy --sdl "{{sdl}}")
    if [ -n "{{image}}" ]; then args+=(--image "{{image}}"); fi
    "${args[@]}"

# ── Akash node (personal LCD/RPC) ────────────────────

# Spin up a personal Akash LCD/RPC node via cosmos-omnibus.
# Bootstraps from the official snapshot, runs PRUNING=nothing forward so it
# becomes archival from the snapshot's block onward. See sdl/akash-node.yaml.
# Tags the deployment "akash-node" for reuse: `just status akash-node`.
up-akash-node:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/up-akash-node-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=up-akash-node finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=up-akash-node started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    deploy_log="$(mktemp -t akash-node-deploy.XXXXXX.log)"
    uv run just-akash deploy --sdl sdl/akash-node.yaml --bid-wait 60 --bid-wait-retry 120 | tee "$deploy_log"
    dseq=$(sed -n 's/.*DSEQ: \([0-9]*\).*/\1/p' "$deploy_log" | head -1)
    rm -f "$deploy_log"
    if [ -n "$dseq" ]; then
        uv run just-akash tag --dseq "$dseq" --name akash-node
        echo
        echo "Akash node deployed. Boot takes ~15-25 min (snapshot stream + extract + sync)."
        echo "Check progress:    just status akash-node"
        echo "Tail node logs:    just connect akash-node lease-shell"
        echo "Once LCD is up:    akash-wallet-audit --api-base http://<host>:1317"
    fi

# Print just the LCD URL of the running akash-node deployment.
# Convenience helper — reads the forwarded LCD endpoint (internal port 1317)
# from `status --json`. Prints nothing until the lease is active and the
# provider has published its forwarded ports.
akash-node-lcd:
    #!/bin/bash
    set -euo pipefail
    status_json="$(uv run just-akash status --json --dseq akash-node 2>/dev/null || true)"
    printf '%s' "$status_json" | python3 -c 'import sys, json; d = json.loads(sys.stdin.read() or "{}"); e = next((x for x in d.get("endpoints", []) if x.get("internal_port") == 1317), None); print("http://" + str(e["host"]) + ":" + str(e["port"])) if e else None'

# Destroy the akash-node deployment.
down-akash-node:
    #!/bin/bash
    set -euo pipefail
    mkdir -p "{{log_dir}}"
    timestamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    log_file="{{log_dir}}/down-akash-node-${timestamp}.log"
    exec > >(tee -a "$log_file") 2>&1
    trap 'status=$?; echo "[INFO] recipe=down-akash-node finished_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") exit_code=${status} log_file=${log_file}"' EXIT
    echo "[INFO] recipe=down-akash-node started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ") cwd=$PWD log_file=$log_file"
    set -x
    uv run just-akash destroy --dseq akash-node

# ── Variables ────────────────────────────────────────
log_dir := ".logs/just"
