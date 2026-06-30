#!/usr/bin/env bash
# Morgoth backup: PostgreSQL (pg_dump) + ChromaDB (tarred persist dir).
# Backups land outside the repo at ~/Morgoth/backups/<timestamp>/.
# Old backups beyond RETENTION_DAYS are pruned ONLY after a successful run.
#
# Consistency note: ChromaDB is a SQLite-backed file store. A truly
# consistent snapshot would require pausing writes (Morgoth's cycle loop).
# For a personal single-process deployment, a hot file copy at a low-activity
# hour (cron 04:00) is acceptable — SQLite WAL-mode reads cleanly and any
# in-flight write is at most one row stale.

set -u  # not -e: we manage failures explicitly so prune doesn't run on a bad backup
set -o pipefail

REPO_DIR="$HOME/Morgoth/morgoth"
BACKUP_ROOT="$HOME/Morgoth/backups"
LOG_FILE="$BACKUP_ROOT/backup.log"
RETENTION_DAYS="${MORGOTH_BACKUP_RETENTION_DAYS:-14}"
CHROMA_DIR="$REPO_DIR/data/chroma_db"

mkdir -p "$BACKUP_ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
DEST="$BACKUP_ROOT/$TS"
mkdir -p "$DEST"

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >>"$LOG_FILE"; }

log "START backup ts=$TS dest=$DEST"

# Load POSTGRES_URL from .env without polluting the environment for children.
if [[ -f "$REPO_DIR/.env" ]]; then
    POSTGRES_URL="$(grep -E '^POSTGRES_URL=' "$REPO_DIR/.env" | head -1 | cut -d= -f2- | tr -d '\r\n')"
else
    log "FAIL no .env at $REPO_DIR/.env"
    exit 1
fi
if [[ -z "${POSTGRES_URL:-}" ]]; then
    log "FAIL POSTGRES_URL empty"
    exit 1
fi

PG_OK=0
CHROMA_OK=0

# --- PostgreSQL -------------------------------------------------------------
log "pg_dump start"
if pg_dump "$POSTGRES_URL" --no-owner --no-privileges 2>>"$LOG_FILE" \
        | gzip > "$DEST/postgres.sql.gz"; then
    PG_SIZE=$(stat -c%s "$DEST/postgres.sql.gz")
    if [[ "$PG_SIZE" -gt 1024 ]]; then
        log "pg_dump OK size=${PG_SIZE}B"
        PG_OK=1
    else
        log "FAIL pg_dump produced suspiciously small file ${PG_SIZE}B"
    fi
else
    log "FAIL pg_dump exit=$?"
fi

# --- ChromaDB ---------------------------------------------------------------
log "chroma tar start dir=$CHROMA_DIR"
if [[ ! -d "$CHROMA_DIR" ]]; then
    log "FAIL chroma dir missing"
elif tar -czf "$DEST/chroma.tar.gz" -C "$(dirname "$CHROMA_DIR")" "$(basename "$CHROMA_DIR")" 2>>"$LOG_FILE"; then
    CH_SIZE=$(stat -c%s "$DEST/chroma.tar.gz")
    if [[ "$CH_SIZE" -gt 1024 ]]; then
        log "chroma tar OK size=${CH_SIZE}B"
        CHROMA_OK=1
    else
        log "FAIL chroma tar produced suspiciously small file ${CH_SIZE}B"
    fi
else
    log "FAIL chroma tar exit=$?"
fi

# --- Retention --------------------------------------------------------------
PRUNED=0
if [[ "$PG_OK" -eq 1 && "$CHROMA_OK" -eq 1 ]]; then
    # Only prune if BOTH halves of this run succeeded. We never delete good
    # old backups when the new one is bad.
    while IFS= read -r -d '' old; do
        rm -rf "$old"
        PRUNED=$((PRUNED + 1))
        log "pruned $old"
    done < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
                  -not -path "$DEST" \
                  -mtime "+$RETENTION_DAYS" -print0)
    log "prune complete count=$PRUNED retention_days=$RETENTION_DAYS"
else
    log "prune SKIPPED (backup did not fully succeed)"
fi

if [[ "$PG_OK" -eq 1 && "$CHROMA_OK" -eq 1 ]]; then
    log "END backup OK ts=$TS"
    exit 0
else
    log "END backup FAILED ts=$TS pg=$PG_OK chroma=$CHROMA_OK"
    exit 1
fi
