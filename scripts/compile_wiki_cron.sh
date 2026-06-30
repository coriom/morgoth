#!/usr/bin/env bash
# Daily Morgoth Wiki compilation. Append stdout (with timestamp) to vault cron.log.
# Designed to be invoked from cron — fails safely if pieces of the environment
# are missing rather than spamming the operator's mailbox.

set -u

REPO_DIR="$HOME/Morgoth/morgoth"
VAULT_DIR="$HOME/Morgoth/vault"
LOG_FILE="$VAULT_DIR/cron.log"

mkdir -p "$VAULT_DIR"

cd "$REPO_DIR" || {
    printf '[%s] ERROR: cannot cd to %s\n' "$(date -Iseconds)" "$REPO_DIR" >>"$LOG_FILE"
    exit 1
}

# shellcheck disable=SC1091
source .venv/bin/activate 2>>"$LOG_FILE" || {
    printf '[%s] ERROR: cannot activate .venv\n' "$(date -Iseconds)" >>"$LOG_FILE"
    exit 1
}

printf '[%s] START compile_wiki\n' "$(date -Iseconds)" >>"$LOG_FILE"
python scripts/compile_wiki.py >>"$LOG_FILE" 2>&1
RC=$?
printf '[%s] END compile_wiki rc=%d\n\n' "$(date -Iseconds)" "$RC" >>"$LOG_FILE"
exit "$RC"
