#!/usr/bin/env bash
# morgoth — user-facing CLI for the Morgoth autonomous research agent.
#
# EXTENSIBILITY CONTRACT
#   Add a new subcommand in EXACTLY THREE PLACES:
#     1. a `cmd_<name>()` function below (one small block)
#     2. a `<name>) cmd_<name> "$@";;` arm in the dispatch `case`
#     3. a `"  <name>   short description"` line in `usage()`
#   `usage()` is the single source of truth for what commands exist.
#
# Service control (start/stop/restart) uses `sudo -n` and relies on the
# NOPASSWD whitelist at /etc/sudoers.d/morgoth. If the whitelist isn't
# installed, `-n` fails fast with a clear message rather than hanging on
# a password prompt.

set -u
set -o pipefail

SERVICE="morgoth.service"
API_BASE="http://localhost:8000"
LOG_FILE="/home/corio/Morgoth/morgoth.systemd.log"
SYSTEMCTL="/usr/bin/systemctl"
READY_WAIT_SECS=30

# ---------- helpers ---------------------------------------------------------

_err() { printf 'morgoth: %s\n' "$*" >&2; }

_sudo_service() {
    # $1 = start|stop|restart
    if ! sudo -n "$SYSTEMCTL" "$1" "$SERVICE" 2>/dev/null; then
        _err "sudo -n systemctl $1 $SERVICE failed."
        _err "The NOPASSWD whitelist at /etc/sudoers.d/morgoth is probably missing."
        _err "See project notes for the one-time install command."
        return 1
    fi
}

_wait_for_ready() {
    local start_ts=$SECONDS
    for _ in $(seq 1 "$READY_WAIT_SECS"); do
        body=$(curl -fs "$API_BASE/api/brain/status" 2>/dev/null || true)
        if [[ -n "$body" ]] && echo "$body" | grep -qE '"ready"[[:space:]]*:[[:space:]]*true'; then
            local elapsed=$((SECONDS - start_ts))
            local model
            model=$(echo "$body" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("primary_model",""))' 2>/dev/null)
            printf 'morgoth: ready in %ss (primary_model=%s)\n' "$elapsed" "$model"
            return 0
        fi
        sleep 1
    done
    _err "not ready after ${READY_WAIT_SECS}s. Check 'morgoth logs' or 'systemctl status morgoth'."
    return 1
}

# ---------- commands --------------------------------------------------------

cmd_start() {
    _sudo_service start || return 1
    _wait_for_ready
}

cmd_stop() {
    _sudo_service stop || return 1
    if [[ "$($SYSTEMCTL is-active "$SERVICE" 2>/dev/null)" == "inactive" ]]; then
        printf 'morgoth: stopped\n'
    else
        _err "service did not reach inactive state; run 'systemctl status $SERVICE'"
        return 1
    fi
}

cmd_restart() {
    _sudo_service restart || return 1
    _wait_for_ready
}

cmd_status() {
    local state
    state=$($SYSTEMCTL is-active "$SERVICE" 2>/dev/null || true)
    printf 'service: %s\n' "$state"
    if [[ "$state" != "active" ]]; then
        return 0
    fi
    body=$(curl -fs "$API_BASE/api/brain/status" 2>/dev/null || true)
    if [[ -z "$body" ]]; then
        printf 'api: not responding on %s (service is active but socket refused)\n' "$API_BASE"
        return 0
    fi
    python3 - <<PY
import json, sys
d = json.loads("""$body""")
print(f"ready:               {d.get('ready')}")
print(f"primary_model:       {d.get('primary_model')}")
print(f"total_cycles:        {d.get('total_cycles_completed')}")
print(f"last_cycle_at:       {d.get('last_cycle_at')}")
print(f"last_cycle_action:   {d.get('last_cycle_action')}")
print(f"uptime_seconds:      {d.get('uptime_seconds')}")
PY
}

cmd_logs() {
    local n=${1:-50}
    if [[ ! -f "$LOG_FILE" ]]; then
        _err "log file $LOG_FILE not found (has the service ever run under systemd?)"
        return 1
    fi
    tail -n "$n" -f "$LOG_FILE"
}

cmd_wiki() {
    if ! body=$(curl -fs -X POST "$API_BASE/api/wiki/compile" 2>&1); then
        _err "wiki compile failed. Is Morgoth running? Try 'morgoth status'."
        return 1
    fi
    echo "$body" | python3 -m json.tool
}

# Self-modify subcommands delegate to `python -m self_modify.cli`. Each
# runs in the repo root using the venv Python so imports resolve.
REPO_DIR="/home/corio/Morgoth/morgoth"
VENV_PY="$REPO_DIR/.venv/bin/python"

cmd_proposals() {
    (cd "$REPO_DIR" && "$VENV_PY" -m self_modify.cli list "$@")
}

cmd_approve() {
    if [[ $# -lt 1 ]]; then
        _err "usage: morgoth approve <proposal_id>"
        return 2
    fi
    (cd "$REPO_DIR" && "$VENV_PY" -m self_modify.cli approve "$1")
}

cmd_reject() {
    if [[ $# -lt 1 ]]; then
        _err "usage: morgoth reject <proposal_id> [--reason \"...\"]"
        return 2
    fi
    (cd "$REPO_DIR" && "$VENV_PY" -m self_modify.cli reject "$@")
}

cmd_show() {
    if [[ $# -lt 1 ]]; then
        _err "usage: morgoth show <proposal_id>"
        return 2
    fi
    (cd "$REPO_DIR" && "$VENV_PY" -m self_modify.cli show "$1")
}

cmd_apply() {
    if [[ $# -lt 1 ]]; then
        _err "usage: morgoth apply <proposal_id>"
        return 2
    fi
    (cd "$REPO_DIR" && "$VENV_PY" -m self_modify.cli apply "$1")
}

cmd_focus() {
    # Three shapes:
    #   morgoth focus              → show
    #   morgoth focus "TEXT"       → set
    #   morgoth focus --clear      → clear
    if [[ $# -eq 0 ]]; then
        (cd "$REPO_DIR" && "$VENV_PY" scripts/focus_cli.py show)
    elif [[ "$1" == "--clear" ]]; then
        (cd "$REPO_DIR" && "$VENV_PY" scripts/focus_cli.py clear)
    else
        (cd "$REPO_DIR" && "$VENV_PY" scripts/focus_cli.py set "$1")
    fi
}

# ---------- usage + dispatch ------------------------------------------------

usage() {
    cat <<'USAGE'
morgoth — control CLI for the Morgoth autonomous research agent.

Usage: morgoth <command> [args]

Commands:
  start           start the systemd service and wait until /api/brain/status is ready
  stop            stop the systemd service
  restart         restart the service and wait until ready
  status          show service state + a compact brain/status summary
  logs [N]        tail the unit log (default 50 lines, follow mode)
  wiki            trigger POST /api/wiki/compile and print the counts
  proposals       list pending self-modify proposals (--recent for history)
  show ID         show one proposal in full (content, rationale, status)
  approve ID      approve a pending_approval proposal
  reject ID       reject a proposal (--reason optional)
  apply ID        apply an approved proposal (writes live tree; the door)
  focus [TEXT|--clear]  set / show / clear the operator focus directive
                        (steers objective-generation topic choice only)
  help            print this message
USAGE
}

case "${1:-help}" in
    start)     shift; cmd_start "$@";;
    stop)      shift; cmd_stop "$@";;
    restart)   shift; cmd_restart "$@";;
    status)    shift; cmd_status "$@";;
    logs)      shift; cmd_logs "$@";;
    wiki)      shift; cmd_wiki "$@";;
    proposals) shift; cmd_proposals "$@";;
    show)      shift; cmd_show "$@";;
    approve)   shift; cmd_approve "$@";;
    reject)    shift; cmd_reject "$@";;
    apply)     shift; cmd_apply "$@";;
    focus)     shift; cmd_focus "$@";;
    help|-h|--help) usage;;
    *)
        _err "unknown command: $1"
        echo
        usage
        exit 2
        ;;
esac
