# Domain contract for the Python engine

```
Project
  ↓ selects one Domain per engine process
Domain ── semantics, entities, windows, metric declarations, scorer IDs
  ↓
Generic Engine ── orchestration, lifecycle, persistence, objectives, routing
  ↓
Tools ── data acquisition; selected by the Domain's declared tool IDs
```

Project owns instance selection, workspace and isolated storage/runtime state, including its LLM profile. Domain owns vocabulary, entity/subject interpretation, semantic windows, metric series collection, source roles and scorer declarations. A scorer/backtest implementation owns its specialist inference and market reference data. Adding a normal data/research domain should not require editing generic core modules. A new tool or genuinely new scorer implementation still needs code and explicit trusted registration; YAML cannot name arbitrary Python imports.

The second-Domain isolation proof required a generic correction: installed tool discovery is separate from Domain-authorized research membership. See [TOOL_RAIL_CONTRACT.md](TOOL_RAIL_CONTRACT.md). Tool acquisition declarations cannot activate a tool implicitly; `rail.tools` is the sole Domain authority.

## Coupling audit and migration boundary

| Location (pre-change) | Crypto assumption | Runtime critical? | Owner after change |
| --- | --- | --- | --- |
| `analysis/thesis_backtest.py:28,154` | `Asset` limited to bitcoin/ethereum and subject inference | Yes for scorer, no for generic core | DOMAIN entity aliases; BACKTEST keeps asset-specific result typing |
| `core/contradictions.py:41-135` | price tokens and 2h/6h windows | Yes | DOMAIN semantic classes/windows; CORE compares durations |
| `core/metric_recorder.py:34-125` | one global-market tool, three fields, CoinPaprika source and 900s cadence | Yes | DOMAIN collector specs/metric maps; TOOL provides payload; CORE schedules/writes |
| `core/brain.py:379-414` | price/history self-tests and recurring BTC task | Yes | DOMAIN bootstrap defaults/task; CORE invokes scheduler/tool router |
| `core/brain.py:1063-1078,1470-1490` | crypto-specific bootstrap and thesis examples | Yes | DOMAIN prompt snippets/examples; CORE retains generic rules |
| `core/objective_gen_context.py` | registered-tool and evidence context | Yes; no fixed market source | CORE with DOMAIN-selected vocabulary/tool registry |
| `core/campaign.py:20-90,261` | crypto examples in comments; title vocabulary; direct descriptive scorer | Yes | DOMAIN stopwords/scorer ID; BACKTEST implementation |
| `analysis/campaign_quality.py`, `analysis/source_attribution.py:147,185` | market measurement classes, source/tool/field maps | Yes for quality, specialist scoring | DOMAIN measurement metadata; BACKTEST/SCORER measurement logic |
| `core/source_cache.py:50-72,264` | configured collector tools/cadences | Yes | DOMAIN config already; CORE generic iteration |
| `core/project.py:66,106-112` | legacy `default` crypto storage adapter | Yes, compatibility only | PROJECT canonical namespace; unchanged |
| `analysis/session_report.py:492-505`; `scripts/backtest_theses*.py`; `scripts/dryrun_objective_gen.py:76` | crypto historical scorer and dry-run examples | No for autonomous loop | BACKTEST/offline script; deferred migration |

`Domain.metric_collections` maps a registered tool to a source label, minimum 60-second cadence, payload fields already named by `metric_field_map`, and tool args. Each declared metric field belongs to exactly one collector. No collectors means no metric recorder or session-gap heartbeat; it never silently polls a crypto tool. `METRIC_RECORDER_INTERVAL_SECS` remains an operator-wide valid override for existing installations. The built-in crypto pack retains the three historical metric names, tool, source, args and 900-second cadence.

`Domain.semantic_classes` maps class names to subject phrases and `semantic_windows_hours` declares every class plus an explicit `default`; `semantic_window_env` names optional existing environment overrides. Unknown class lookup fails. Entity aliases have optional required/excluded phrases. The crypto pack retains substring classification and its existing exclusions. Legacy `price_class_tokens` is accepted only as an input adapter for old packs; the built-in pack uses the new canonical class declaration. A pack without windows cannot ask the generic contradiction detector for one.

`analysis.scorer_registry` is a trusted implementation registry. A Domain names zero or more scorer IDs by role; absent means no specialist scorer is selected. The registry does not import scorer modules until selection. The crypto pack explicitly opts into its directional, descriptive and campaign-quality implementations. This keeps crypto backtests specialised rather than pretending their market references are universal.

## Remaining intentional exceptions

- `core/domain.py:44` and `core/project.py:106-112` reserve the historical default crypto installation and paths. This is a compatibility boundary, not a new Domain semantic default.
- `core/metric_recorder.py:69-76` retains three import-stable metric constants for existing crypto scorers and tests; collection behavior reads Domain specs.
- `core/brain.py` and `core/campaign.py` retain historical crypto references in comments explaining regression incidents; runtime prompt examples are pack-owned.
- `core/numeric_fidelity.py:45` still imports the shared number parser from the descriptive scorer. Moving that parser requires a separate fidelity-preserving change; numerical gate semantics are unchanged here.
- `analysis/session_report.py:492-505` and `scripts/backtest_theses*.py` are offline crypto reporting/backtest entry points. `scripts/dryrun_objective_gen.py:76` is an offline crypto demonstration. They are not selected by an ordinary non-crypto Domain process.
- `analysis/campaign_quality.py` remains the explicit crypto campaign scorer. Its Deribit source/unit/fidelity measurement rules are unchanged.

The test-only neutral Domain uses alpha/beta entities, fast/slow windows and one synthetic metric. It does not register a scorer, crypto tool or crypto Project namespace. `tests/test_generic_domain_contract.py` also rejects executable BTC/ETH/Binance/Deribit literals in curated generic modules.
