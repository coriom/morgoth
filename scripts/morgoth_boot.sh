#!/usr/bin/env bash
# Morgoth boot / recovery script — LF line endings only.
#
# Idempotent: safe to re-run. Verifies the full dependency chain is up in
# order and waits for each with explicit health checks; PM2 resurrects
# Morgoth from ~/.pm2/dump.pm2 if the API isn't already responding.
#
# systemd already brings up postgresql and ollama at WSL start (both units
# are enabled). This script is the ordering/wait wrapper — the safety net
# for the racy case where the pm2 systemd unit fires before postgres or
# ollama are actually accepting connections, and the mechanism for Morgoth
# itself when the pm2-<user> unit is not (yet) installed.
#
# Logs every step to ~/Morgoth/boot.log.

set -u
set -o pipefail

LOG="$HOME/Morgoth/boot.log"
mkdir -p "$(dirname "$LOG")"

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG" >&2; }

# Pin nvm-installed pm2 explicitly. Boot contexts (systemd unit, wsl.conf
# [boot] command, Task Scheduler) DO NOT source ~/.bashrc, so nvm's PATH
# injection is absent — we must add it here.
PM2_BIN="/home/corio/.nvm/versions/node/v24.15.0/bin/pm2"
NODE_BIN_DIR="/home/corio/.nvm/versions/node/v24.15.0/bin"
export PATH="$NODE_BIN_DIR:$PATH"

log "=== morgoth_boot start (pid=$$) ==="

wait_until() {
    # $1: seconds, $2..: test command
    local secs=$1; shift
    for _ in $(seq 1 "$secs"); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    return 1
}

# --- 1. PostgreSQL ----------------------------------------------------------
if pg_isready -h localhost -p 5432 -q; then
    log "postgres: already accepting connections"
else
    log "postgres: not up — 'sudo -n /usr/bin/systemctl start postgresql'"
    # -n: non-interactive. Succeeds silently via the NOPASSWD whitelist in
    # /etc/sudoers.d/morgoth. If the whitelist is missing, -n fails fast
    # (rather than hanging on a password prompt at boot) and we fall through
    # to the wait loop, which will fail loudly.
    sudo -n /usr/bin/systemctl start postgresql 2>>"$LOG" || \
        log "postgres: sudo -n systemctl start returned non-zero (continuing to wait)"
    if wait_until 30 pg_isready -h localhost -p 5432 -q; then
        log "postgres: ready"
    else
        log "FAIL postgres not accepting connections after 30s"
        exit 1
    fi
fi

# --- 2. Ollama --------------------------------------------------------------
if curl -fs -o /dev/null http://localhost:11434/api/tags; then
    log "ollama: already responding"
else
    log "ollama: not up — 'sudo -n /usr/bin/systemctl start ollama'"
    sudo -n /usr/bin/systemctl start ollama 2>>"$LOG" || \
        log "ollama: sudo -n systemctl start returned non-zero (continuing to wait)"
    if wait_until 30 curl -fs -o /dev/null http://localhost:11434/api/tags; then
        log "ollama: ready"
    else
        log "FAIL ollama not responding after 30s"
        exit 1
    fi
fi

# --- 3. Morgoth via PM2 -----------------------------------------------------
if curl -fs -o /dev/null http://localhost:8000/api/brain/status; then
    log "morgoth: /api/brain/status already responding"
else
    if [[ ! -x "$PM2_BIN" ]]; then
        log "FAIL pm2 binary missing at $PM2_BIN"
        exit 1
    fi
    if [[ ! -f "$HOME/.pm2/dump.pm2" ]]; then
        log "FAIL no PM2 dump at ~/.pm2/dump.pm2 — run 'pm2 save' from a live session"
        exit 1
    fi
    log "morgoth: resurrecting from ~/.pm2/dump.pm2"
    "$PM2_BIN" resurrect 2>&1 | tee -a "$LOG" >&2 || {
        log "FAIL pm2 resurrect"
        exit 1
    }
    # Morgoth is heavier to start (Python import, DB pool, embedding model
    # load). Give it up to 90s to answer.
    if wait_until 90 sh -c 'curl -fs http://localhost:8000/api/brain/status 2>/dev/null | grep -qE "\"ready\"[[:space:]]*:[[:space:]]*true"'; then
        log "morgoth: ready=true"
    else
        body=$(curl -fs http://localhost:8000/api/brain/status 2>/dev/null || echo "(no response)")
        log "FAIL morgoth /api/brain/status not ready=true after 90s. last body: $body"
        exit 1
    fi
fi

log "=== morgoth_boot OK ==="
