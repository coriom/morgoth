# Restoring Morgoth from a backup

Backups land in `~/Morgoth/backups/<TIMESTAMP>/` and contain two files:

- `postgres.sql.gz` — full `pg_dump` of the Morgoth database.
- `chroma.tar.gz` — tar of the ChromaDB persist directory.

The current backup schedule is **catch-up on startup** (fires when the
latest backup exceeds `MORGOTH_BACKUP_MAX_AGE_HOURS`, default 24 h)
plus the legacy `0 4 * * * scripts/backup_morgoth.sh` cron. Retention:
keep the last `MORGOTH_BACKUP_RETENTION_COUNT` (default 7) directories
AND anything younger than `MORGOTH_BACKUP_RETENTION_DAYS` (default 14).

## Full restore into a fresh scratch database (RECOMMENDED)

Runs the dump into a NEW database — the safest drill and the one you
want under stress. Requires `sudo` (to run as the `postgres` role,
which owns `CREATEDB`).

```bash
TS=20260921_142152                              # pick the backup dir
BAK=~/Morgoth/backups/$TS

# 1. Create the scratch database (postgres role → CREATEDB).
sudo -u postgres createdb morgoth_restore

# 2. Grant morgoth_user access to the scratch DB (so the app could
#    point at it if the operator wants to swap DBs, and so the psql
#    commands below can use the app's credentials).
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE morgoth_restore TO morgoth_user;"

# 3. Restore the pg_dump.
zcat $BAK/postgres.sql.gz \
  | psql "postgresql://morgoth_user:PASSWORD@localhost:5432/morgoth_restore"

# 4. Sanity check — verify every core table exists and has data.
psql "postgresql://morgoth_user:PASSWORD@localhost:5432/morgoth_restore" -c "\dt"
psql "postgresql://morgoth_user:PASSWORD@localhost:5432/morgoth_restore" -tAc "
  SELECT table_name FROM information_schema.tables
  WHERE table_schema='public' ORDER BY table_name;
"
psql "postgresql://morgoth_user:PASSWORD@localhost:5432/morgoth_restore" -tAc "
  SELECT
    (SELECT COUNT(*) FROM theses)          AS theses,
    (SELECT COUNT(*) FROM objectives)      AS objectives,
    (SELECT COUNT(*) FROM metric_series)   AS metric_series,
    (SELECT COUNT(*) FROM source_snapshots) AS source_snapshots;
"

# 5. Restore ChromaDB (only if you want the vector store back too).
#    Stop the running service first; Chroma is a file store.
sudo systemctl stop morgoth.service
tar -xzf $BAK/chroma.tar.gz -C /tmp/                 # dry-run into /tmp
diff -qr /tmp/chroma_db ~/Morgoth/morgoth/data/chroma_db | head
# When ready:
tar -xzf $BAK/chroma.tar.gz -C ~/Morgoth/morgoth/data/
sudo systemctl start morgoth.service

# 6. Drop the scratch DB (once you've verified the data).
sudo -u postgres dropdb morgoth_restore
```

## Drill without sudo (schema-scoped, no CREATEDB)

When `sudo` isn't available, restore into a `restore_drill` SCHEMA inside
the live DB. Rewrites `public.` references in the dump. Data lives in
the same DB but a different namespace; the live schema is untouched.

```bash
TS=20260921_142152
BAK=~/Morgoth/backups/$TS
PG_URL="postgresql://morgoth_user:PASSWORD@localhost:5432/morgoth"

# 1. Create the scratch schema.
psql "$PG_URL" -c "CREATE SCHEMA IF NOT EXISTS restore_drill;"

# 2. Rewrite the dump: point every 'public.' reference at 'restore_drill.'
#    and set the search_path so unqualified references land there too.
zcat $BAK/postgres.sql.gz \
  | sed 's|public\.|restore_drill.|g;
         s|SET search_path = "\?$user"\?, public;|SET search_path = restore_drill;|' \
  > /tmp/restore_drill.sql

# 3. Restore.
psql "$PG_URL" -q -f /tmp/restore_drill.sql

# 4. Row-count parity check (live vs restored).
psql "$PG_URL" -c "
  SELECT 'theses' AS t, (SELECT COUNT(*) FROM public.theses)
                     , (SELECT COUNT(*) FROM restore_drill.theses)
  UNION ALL SELECT 'objectives'
                     , (SELECT COUNT(*) FROM public.objectives)
                     , (SELECT COUNT(*) FROM restore_drill.objectives);
"

# 5. Drop the scratch schema when done.
psql "$PG_URL" -c "DROP SCHEMA restore_drill CASCADE;"
```

## Actual drill run (2026-09-21) — proof it works

```
Backup: ~/Morgoth/backups/20260921_142152/  (pg 810 KB + chroma 21.8 MB)

Row-count parity (live vs restored, schema-scoped drill):
  theses                    1091 | 1091
  objectives                 504 |  504
  metric_series              277 |  277
  source_snapshots            53 |   53
  campaigns                    1 |    1
  numeric_fidelity_events    334 |  334
  session_gaps                 5 |    5
  web_search_cache             1 |    1

30 tables present in the restore. DROP SCHEMA restore_drill CASCADE
completed with 30 objects. Live DB untouched.
```
