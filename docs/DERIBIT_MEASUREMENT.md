# Deribit coverage and source/unit instrument

This is a measurement change, not an extraction or fidelity-gate change. Initial
backend HEAD was `5a3355a1237bb949e2d45859132578fb380cd5f1`, clean; UI was
`a11ff08` and is untouched. No dependencies were added.

## Audit before changes

The following line references describe the initial revision, not shifted lines
in this patch. All Domain structures remain declarative in `domains/crypto/domain.yaml`.

| Structure / consumer | Initial reference | Initial Deribit coverage |
| --- | --- | --- |
| Served vocabulary | pack:203; `analysis/campaign_quality.py:465` | Explicit map absent; discovery inferred some digest/description words |
| Rail field contract | pack:332; `analysis/rail_health.py:35` | Absent |
| Direction rules, metric families | pack:355,365,377 | Existing funding family; no Deribit-specific numeric rule |
| Scope rules | pack:390; `analysis/campaign_quality.py:230` | Global-market versus asset scope only; no exchange attribution |
| Field phrases / ambiguity | pack:399,474; `core/field_confusion.py:51` | Deribit absent; single-field resolver declines tied phrases |
| Single numeric field / timestamps | pack:477,482 | Deribit is multi-field; no exception needed |
| Recorder sources / price fields | pack:496; backtest markers pack:512 | Existing Coinbase/Binance selection; intentionally unchanged, not a universal rail coverage map |
| Cache schedule / default arguments | pack:564,574; `core/source_cache.py:264` | Deribit absent; tool itself needs no arguments |
| Tool source identities / field units / exemptions | `core/domain.py:71` | No declared maps existed |
| Pack loading / Project selection | `core/domain.py:228,273`; `core/project.py:140,161` | Project selects Domain; cached once per process; unchanged ownership |

The 11-field tool contract already exists at
`tools/data_feeds/get_deribit_btc_perpetual.py:27`; its implementation is unchanged.
Seven prices and open interest are USD, volume is BTC, both funding fields are
fractions. The frozen ticker-response fixture exercises its real extraction via
`httpx.MockTransport`, all 11 keys and numeric types, with no live HTTP. Fixture
values are synthetic public-response-shaped values, not a claimed live capture.

The pack now declares every Deribit field in rail, phrase, unit and semantic-context
maps, plus served vocabulary, source identity and cache schedule. Generic funding
allows current OR 8h; generic price allows mark OR index OR last. Specific phrases
remain specific. The plural resolver uses word boundaries and longest phrases;
the old singular resolver still declines ambiguity.

Both funding collectors use interval 1800 s / max age 7200 s. They do **not**
guarantee simultaneous observations: `core/source_cache.py:261` awaits each due
source sequentially and marks its completion independently. Equal intervals can
drift across engine ticks. No synchronization code changed.

`analysis/measurement_coverage.py` discovers data-source class metadata without
instantiating or executing tools. It checks rail fields, phrases, units, contexts,
served vocabulary, source identity/aliases and cache registration; explicit per-map
tool/field exemptions require reasons. Both quality and rail render warnings;
health/gate verdicts are unchanged. There are zero Deribit blind spots and 11
remaining warnings on other existing tools. These are surfaced, not silently
exempted. Tests remove Deribit from each map and verify detection and exemption
subtraction. Recorder/backtest source selection is deliberately not required for
every rail tool.

## Attribution and units

Existing scope attribution (`analysis/campaign_quality.py:230` initially) catches
global-market evidence used for BTC asset-level metrics, except dominance.
Existing other-subject value (`:265`) compares an evidence tool's numeric values
against other tools, skips values below 1, and uses 0.1% tolerance without a
corresponding-field constraint. Neither class models exchange identity in a claim.
They remain unchanged; `source_misattribution` is a distinct clause/source measure.

The fidelity gate (`core/numeric_fidelity.py:188,246`) uses the evidence tool's
current-cycle payload, not all objective tools. Its parser and 1% tolerance are
reused by the new instrument. It does not convert percent: `0.01%` against fraction
`0.0001` still produces DROP; direct `0.0001%` can pass numerically. The separate
unit instrument flags that latter mistake without changing a gate verdict.

For each fidelity-parsed number:

1. Split comparison/coordination clauses, preserving decimals and grouping commas.
   Exactly one declared proper source alias attributes the number.
2. Otherwise use the evidence item containing that number and its declared tool
   identity. Multiple possible evidence identities remain ambiguous.
3. Otherwise retain UNATTRIBUTED. Never infer identity from generic prose.

Field phrases are local to the number, with preceding metric context for ellipsis
and then subject fallback. Only corresponding declared fields from that objective's
recorded successful readings are numerical references; failed reads are attempts,
not fabricated values. Missing historical records mean UNVERIFIED, not proof of
an unread source. Existing historical classifiers keep their previous latest-global
snapshot policy so unrelated results remain comparable.

An own-source valid field always wins. Matching accepts the fidelity tolerance OR
the reference rounded to written precision (Decimal half-even); `0.000104` thus
matches `0.0001`, while the exact `0.00005` tie does not. A source mismatch needs
another source's corresponding match, or a known read-set lacking the named source.
Derived spread/difference claims retain attribution but are not guessed to be raw
field values. No spread conversion is invented.

Only explicit field units authorize unit checks: USD/BTC swaps and direct
fraction-as-percent. Fraction to percent multiplication by 100 is explicit;
currency/asset conversion is never inferred. Explicit magnitude suffixes reuse
the fidelity parser's scale mechanism. Unknown units are not guessed.
`cross_source` requires two validated sources in the same semantic context AND
field unit. Price subtypes (mark/index/last/bid/ask/24h high/24h low)
remain distinct, so a high cannot be attributed to another source's mark field.
Wrong or unverifiable numbers do not qualify.

Counts are distinct (value, source, semantic context, display unit) citations;
`cross_source` counts theses. Gate rewrite events are retained by objective,
subject and old/new value; this identifies candidate rewritten citations but does
not claim exact event-to-thesis causality where historical linkage is absent.

## Frozen historical corpus proof

The normal campaign CLI was NOT run: `scripts/campaign_cli.py:169` initializes
writable storage; its quality branch can persist gaps, and the scorer initializes
Chroma for old fallback lookup. Instead, a repeatable-read, read-only PostgreSQL
transaction captured only the two historical campaigns, their references and the
47-member control (excluding active-campaign objectives). Existing Chroma metadata
was read with SQLite `mode=ro`, restricted to historical/control objective IDs;
no Chroma initialization, embeddings, inference or writes. This one-off snapshot
read is not a new runtime storage architecture.

The exact same frozen snapshot was replayed before and after through `score_campaign`
with in-memory storage adapters and sockets disabled. The Chroma adapter supplies
recorded same-objective documents, not a fresh embedding-ranked retrieval. Each campaign also had 10 persisted data-gap rows, which were never rewritten.
Full private snapshots remain outside Git; tests retain only numeric control references
and the three short numeric examples below.

| Metric | 2b212b0c before → after | 2a6c0674 before → after |
| --- | --- | --- |
| Objectives / theses | 84 / 257 → same | 27 / 92 → same |
| Old errors | direction 2, scope 3 → same | scope 1, unsourced 2 → same |
| Other old error classes | 0 → 0 | 0 → 0 |
| Fragmentation extras | 55 → 55 | 18 → 18 |
| 0.0001 control genuine / confused | 31 / 16 → 31 / 16 | 31 / 16 → 31 / 16 |
| Unverifiable objectives | 62/84 (73.81%) → same | 10/27 (37.04%) → same |
| Data gaps (top themes and counts) | 10 entries, exactly unchanged | 10 entries, exactly unchanged |
| cross_source | not measured → 0 | not measured → 0 |
| source_misattribution | not measured → 0 | not measured → 0 |
| unit_mismatch | not measured → 3 | not measured → 0 |
| Unattributed citations | not measured → 0 | not measured → 4 |
| Citations associated with gate rewrites | not measured → 6 | not measured → 10 |

Manual inspection of exactly three real unit errors (all from the first campaign):

| Thesis ID | Citation | Attributed source / reference | Verdict / inspection |
| --- | --- | --- | --- |
| 8a458d80-7b76-41c3-a014-777fe0a9aebe | 0.00009654% | Binance / lastFundingRate=0.00009654 fraction | unit_mismatch / true positive |
| d0e1e76a-e817-4bbe-98e4-77fedc9acb56 | 0.00009654% | Binance / lastFundingRate=0.00009654 fraction | unit_mismatch / true positive |
| 24baa3d1-0e7c-46b6-acef-e8bafb4d3637 | 0.00010000% | Binance / lastFundingRate=0.0001 fraction | unit_mismatch / true positive |

These should be 0.009654%, 0.009654%, and 0.01%. The last remains numerically
genuine in the old control. No source errors exist here to manually inspect.
Observed false positives: 0/3 inspected unit detections, not a population estimate.

Snapshot SHA-256 (private local artifacts):

- corpus: `56bef6134f58485a81b53c1d31469e400cd69a37bc7f173deaa8b56065999fb8`
- historical documents: `332710d4c871a1f08bc4e5d0e88515b010fee06874dba5a70079e222220a62d2`
- before report: `fc05fd832456b6392f20f99122f39e14ddbf2468f291aebdc45b42f5e790995f`
- after report: `688995e7606acdbbda4081444c1ec0d73a8e8e313a481161446f19eb860e5fca`

## Runtime loading audit (paths unchanged)

- `core/domain.py:228,292`: YAML read on `_load`; `current_domain` cached once.
  `core/project.py:108,140,160`: legacy/catalog validation can reread packs/manifests;
  current Project is cached. No production reload hook is invoked by this patch.
- `core/brain.py:450,788,798`, `core/tool_router.py:122`: lazy cache/fidelity/field
  imports see disk on first import only; existing modules/constants remain cached.
  This is why a running process must not be assumed to have adopted this patch.
- `tools/discovery.py:42,63`: package enumeration sees added files; importlib caches
  existing modules. Fresh modules or fresh processes can see changed files.
- `api/routes/wiki.py:31,47`: compiler imported once, compile endpoint can discover
  tools lazily (`scripts/compile_wiki.py:401,413`) and inspect source provenance
  (`:679`). Vault reads (`api/routes/wiki.py:197,294`) see regenerated files.
  `scripts/compile_wiki_cron.sh:26` launches a fresh interpreter against the checkout.
  No wiki compilation was triggered here.
- Fresh subprocess paths: `core/backup_watchdog.py:136` (backup script),
  `self_modify/code_tester.py:72`, `self_modify/canonical_runner.py:76`,
  `self_modify/gates.py:609`, `self_modify/artifact_runner.py:106` and artifact import
  (`self_modify/_artifact_harness.py:64`), `self_modify/apply.py:220`,
  `self_modify/diff_logger.py:97`. They may read current files when invoked; no
  proposal/application path was invoked. Existing human gate 3 is unchanged.
- Existing file/code tools (`tools/file_manager.py:35`, `tools/code_executor.py:56`)
  can read/execute within their existing permission boundary; unchanged.
  Provider/health subprocesses (`core/llm/codex_cli.py:85`, `heartbeat.py:75,95`,
  `environment.py:90,157`, `self_modify/reflect_llm.py:289`) launch current binaries;
  they do not reload cached Domain objects. No reflect or provider activation here.
- `core/version.py:28` reads Git once per process; API token reads at
  `api/token.py:46,63` and config/permissions disk reads retain existing behavior.
  No service code reload or lifecycle command was issued.

Read-only campaign state reported active, ending 2026-09-30 05:57:59 UTC. Separate
system and user `systemctl show` checks reported inactive/dead; actual execution is
therefore unconfirmed, not repaired or restarted. No active campaign content was
used in the corpus. This patch equips the later Q1/Q2/Q3 reading; it does not
claim an outcome for the still-open campaign.

Validation: canonical suite **1802 passed (+64), 2 skipped, 0 failed**, wall 15.3 s,
exit 0. Focused Deribit + Project/namespace suite **132 passed**, wall 6.9 s.
Existing crypto price/cache/rate-limit/history, thesis/fidelity/backtest/quality and
session-report tests ran in the canonical suite. A direct host-focused run stalled
and was terminated; the successful rerun used the canonical sandbox. No live rail
check, production test write, service restart, `.env` access, UI edit or Codex safety
activation. Convenience formatting and other tools' metadata completion are deferred.
