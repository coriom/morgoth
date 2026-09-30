# Archive and rerun the Deribit campaign

Campaign `e6563e60-5268-4ef3-81a3-8ca92b6eab05` is **PARTIAL, forensic-only**.
Its intended window was 2026-09-29 13:57:59.719767+08 through
2026-09-30 13:57:59.720707+08. The recorded service stopped at 16:31:42
on September 29; 15 objectives, 73 objective cycles and 44 theses survived.
It must **never** answer Q1/Q2/Q3. The commands below are an operator runbook,
not an instruction to restart during development. They were not executed here.

## Persistence audit (line references at b55447f)

- `memory/persistent.py:505,517,1824,1845`: campaign UUID → objectives.campaign_id;
  theses.objective_id is a text identifier, not a declared foreign key.
- `core/brain.py:1180,1195`: raw cycle tool results live inside objective evidence
  as cycle_payload entries; synthesis/other findings remain in that same array.
  Preserve array order, cycle counter and evidence indices; do not invent times.
- `memory/persistent.py:340,361,294`: campaign_data_gaps.campaign_id is text;
  numeric_fidelity_events links by objective_id, field_confusion_events by thesis_id.
- `memory/persistent.py:445`: contradictions link two thesis UUIDs. Export only
  pairs fully owned by the campaign; do not follow an external thesis reference.
- `memory/persistent.py:680,1523`; `core/brain.py:856`: producer code_version is
  stamped on theses, nullable for older data. Exporter HEAD is NOT producer evidence.
- `memory/persistent.py:203,226,270,384`: session gaps, collector snapshots, metric
  series and connectivity are global. Time overlap does not establish ownership.
  Provider/resource logs and unlinked measurement events are likewise excluded.
- Chroma findings include truncated semantic copies (`core/brain.py:1180`); there
  is no qualified read-only client initialization path here. The authoritative
  PostgreSQL payloads are included; Chroma reconstruction is explicitly unavailable.

## Archive contract

`morgoth campaign --archive UUID --output FILE.json [--validity PARTIAL] [--forensic-only]`

The archive dispatch precedes normal configuration/store initialization
(`scripts/campaign_cli.py`); no PM.initialize, DDL, Chroma, LLM or HTTP is invoked.
A direct asyncpg connection uses read-only defaults, UTC, the canonical Project
namespace and a repeatable-read **read-only transaction**. No public-schema fallback.
Legacy database configuration follows existing dotenv precedence without creating
runtime directories. Credentials/configuration are never included or printed.

Schema version 1 includes the exact persisted campaign row, owned objectives,
theses, indexed evidence/payload projections, optional owned gaps/measurement
ledgers/contradictions, record counts, versions actually present, exporter version,
Project id, export timestamp, operator validity annotation and explicit missing
categories. Arrays have deterministic ordering; evidence arrays retain their
original sequence. Frozen inputs and export timestamp give identical bytes;
otherwise campaign content remains identical while exported_at changes.
Validity is an operator annotation, not a fabricated continuity verdict. The archive
cannot recover missing cycle timestamps, unknown producer versions or global uptime.

Secret-like keys, credential assignments, bearer/private keys and credential-bearing
URLs cause rejection of the whole export, not silent redaction of evidence. This is
conservative pattern detection, not proof about arbitrary free-form text. Keep the
artifact private and review before sharing. Source configuration, auth files and
unrelated rows are never archive inputs.

FILE.json and FILE.json.sha256 are published from 0600 temporary files using fsync
and Linux atomic no-replace rename. Existing files/symlinks refuse; no overwrite flag.
Checksum publishes first, then JSON as completion marker, with directory fsyncs.
The pair cannot be one filesystem transaction: interruption can leave a checksum
without JSON. Such a retry refuses; inspect the incomplete artifact rather than
silently overwriting it. Other platforms fail closed until this primitive is qualified.

## Recovery invariant

Previously: stale sweep → orphan reclaim (`core/brain.py:257,287`) → schedule
first autonomous cycle (`:313`) → expire campaigns (`:481`) → claim (`:639`).
A stopped campaign's objective could become pending before expiry, and the old
claim query (`memory/persistent.py:974`) ignored campaign lifecycle entirely.

The order is unchanged; eligibility is now structural before any recovery mutation.
`core/campaign_lifecycle.py` is the single SQL definition: active status, no ended_at,
and ends_at strictly after the database transaction time. The same definition guards
stale sweep, orphan recovery, claiming, active campaign selection and expiry.

Live campaign + stale objective and non-campaign objectives retain their existing
recovery/timeouts. Expired, closed, inconsistent or missing campaigns cannot have
objectives reclaimed/claimed. Their original objective status, evidence, cycle count
and timestamps stay untouched: neither successful completion nor a fabricated stale
verdict describes these forensic rows. This also protects pending and freshly
in-progress legacy rows, even if expiry processing has not run or failed.
Campaign expiry itself keeps the existing completed/ended_at behavior; archive the
original campaign row first. No ORPHAN_RECLAIM_MINUTES workaround is required.

## Future operator sequence (stop on any failed check)

Use the commit accompanying this document, clean and equal to origin/main. Do not
substitute b55447f: that revision contains the recovery defect. The command below
resolves this runbook's commit as the expected release; confirm it matches the
published handoff hash. Execute only after September 30 at 13:58 UTC+08.

```bash
cd ~/Morgoth/morgoth
source .venv/bin/activate
set -euo pipefail
expected=$(git log -1 --format=%H -- docs/DERIBIT_RERUN.md)
git fetch origin main
test "$(git rev-parse HEAD)" = "$expected"
test "$(git rev-parse origin/main)" = "$expected"
test -z "$(git status --porcelain)"
test "$(date +%s)" -ge "$(date -d '2026-09-30 13:58:00 +0800' +%s)"
test -z "${ORPHAN_RECLAIM_MINUTES+x}"
test ! -e /run/systemd/system/morgoth.service.d/forensic-no-reclaim.conf
export MORGOTH_PROJECT=default
umask 077
archive_dir=$(mktemp -d "$HOME/Morgoth/deribit-rerun.XXXXXX")
old=e6563e60-5268-4ef3-81a3-8ca92b6eab05
morgoth campaign --archive "$old" --output "$archive_dir/old.json" --validity PARTIAL --forensic-only
(cd "$archive_dir" && sha256sum -c old.json.sha256)
chmod 0400 "$archive_dir/old.json" "$archive_dir/old.json.sha256"
sudo chattr +i "$archive_dir/old.json" "$archive_dir/old.json.sha256"
# Normal service startup: no timeout/environment/systemd override.
morgoth restart
systemctl is-active --quiet morgoth.service
morgoth status
curl -fsS http://localhost:8000/api/brain/status > "$archive_dir/health.json"
python -c 'import json,sys; assert json.load(open(sys.argv[1]))["ready"] is True' "$archive_dir/health.json"
morgoth rail-check | tee "$archive_dir/rail.txt"
rg -q '^\s*get_bitcoin_futures_funding\s+OK\b' "$archive_dir/rail.txt"
rg -q '^\s*get_deribit_btc_perpetual\s+OK\b' "$archive_dir/rail.txt"
# Expiry may update the campaign row; every old objective must remain identical.
morgoth campaign --archive "$old" --output "$archive_dir/old-after-start.json" --validity PARTIAL --forensic-only
python - "$archive_dir" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
a = json.loads((root / 'old.json').read_text())
b = json.loads((root / 'old-after-start.json').read_text())
assert a['records']['objectives'] == b['records']['objectives'], 'Old objective changed: STOP'
assert b['campaign']['status'] != 'active', 'Old campaign still active: STOP'
assert a['project_id'] == b['project_id'] == 'default'
PY
test "$(git rev-parse HEAD)" = "$expected"
morgoth campaign 'BTC positioning across Binance and Deribit' --days 1 | tee "$archive_dir/new-start.txt"
new_id=$(awk '/^campaign started:/{print $3}' "$archive_dir/new-start.txt")
test -n "$new_id"
morgoth campaign --archive "$new_id" --output "$archive_dir/new-started.json"
python - "$archive_dir" "$new_id" "$expected" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1]); artifact = json.loads((root / 'new-started.json').read_text())
c = artifact['campaign']; assert c['campaign_id'] == sys.argv[2]
manifest = dict(campaign_id=c['campaign_id'], code_version=sys.argv[3],
                started_at=c['started_at'], ends_at=c['ends_at'], project_id=artifact['project_id'])
(root / 'run-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
PY
```

Rail-check writes its own health ledger and calls external tools: it is intentionally
prescribed only after the old window, before the new campaign, never in development.
Its exit code alone is insufficient; the two explicit OK checks above are required.
Do not proceed if readiness, code version, Project or corpus comparison differs.
After the new window, verify continuity, archive the new campaign, and use ONLY that
new ID for Q1/Q2/Q3. Do not reinterpret the old PARTIAL archive as a 24-hour experiment.

Autostart remains **disabled**. This is an operational risk: WSL/system shutdown
interrupts research and a subsequent boot will not resume the service automatically.
Arrange an uninterrupted machine session and external monitoring for the full 24h;
if it is interrupted, reassess continuity instead of silently resuming the experiment.
No startup setting, service state, production campaign or runtime override was changed
by this implementation. No new dependencies; UI, Project/Domain and Codex lock unchanged.
