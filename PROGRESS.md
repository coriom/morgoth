# PROGRESS.md — Morgoth Development Tracker

> Updated by Codex after each completed deliverable.
> Updated by human after each review, test, or decision.

---

## Current Status

**Phase**: 3 — Intelligence Expansion  
**Overall**: Phase 1 stable, Phase 2 implemented, Phase 2b backend endpoints complete, Phase 3 Steps 1-6 plus agent and tool-router registration complete  
**Last updated**: 2026-05-12 by Codex — Cycle-cap auto-completion: cycle_count column added to objectives, increment_cycle_count() in PersistentMemory, force-complete after MAX_CYCLES_PER_OBJECTIVE in run_autonomous_cycle(), directive no-objectives prompt, MAX_CYCLES_PER_OBJECTIVE config field  
**Next action**: Human to `pm2 restart morgoth`, wait 4-5 cycles (~40 min), then check `curl http://localhost:8000/api/objectives | python3 -m json.tool` — original objective should be status='done' with auto_completed=true in evidence; at least one new objective should exist

---

## Phase 1 — Backend (morgoth/)

### Core
| File | Status | Notes |
|---|---|---|
| `core/config.py` | ⬜ Todo | |
| `core/llm_client.py` | ⬜ Todo | |
| `core/tool_router.py` | ⬜ Todo | |
| `core/scheduler.py` | ⬜ Todo | |
| `core/brain.py` | ⬜ Todo | |

### Memory
| File | Status | Notes |
|---|---|---|
| `memory/working.py` | ⬜ Todo | |
| `memory/episodic.py` | ⬜ Todo | ChromaDB |
| `memory/persistent.py` | ⬜ Todo | PostgreSQL via asyncpg |

### Tools
| File | Status | Notes |
|---|---|---|
| `tools/base_tool.py` | ⬜ Todo | Interface contract |
| `tools/web_search.py` | ⬜ Todo | DuckDuckGo |
| `tools/code_executor.py` | ⬜ Todo | Sandboxed subprocess |
| `tools/file_manager.py` | ⬜ Todo | EVOLVABLE ZONE only |
| `tools/data_feeds/crypto.py` | ⬜ Todo | CoinGecko public API |
| `tools/data_feeds/finance.py` | ⬜ Todo | Yahoo Finance / FRED |
| `tools/data_feeds/news.py` | ⬜ Todo | RSS feeds |
| `tools/remember.py` | ⬜ Todo | Write to ChromaDB |
| `tools/recall.py` | ⬜ Todo | Query ChromaDB |
| `tools/create_agent.py` | ⬜ Todo | Calls agent_manager |
| `tools/notify.py` | ⬜ Todo | Telegram wrapper |

### Agents
| File | Status | Notes |
|---|---|---|
| `agents/base_agent.py` | ⬜ Todo | Interface contract |
| `agents/agent_manager.py` | ⬜ Todo | Lifecycle management |

### Notifications
| File | Status | Notes |
|---|---|---|
| `notifications/telegram.py` | ⬜ Todo | Bot + chat_id from .env |

### API
| File | Status | Notes |
|---|---|---|
| `api/server.py` | ⬜ Todo | FastAPI app, port 8000 |
| `api/routes/chat.py` | ⬜ Todo | |
| `api/routes/agents.py` | ⬜ Todo | |
| `api/routes/market.py` | ⬜ Todo | |
| `api/routes/brain.py` | ⬜ Todo | |
| `api/routes/admin.py` | ⬜ Todo | |
| `api/ws/handler.py` | ⬜ Todo | WebSocket manager |

### Entry Points & Config
| File | Status | Notes |
|---|---|---|
| `main.py` | ⬜ Todo | AWAKENING protocol |
| `scripts/init_db.py` | ⬜ Todo | PostgreSQL table creation |
| `requirements.txt` | ⬜ Todo | |
| `MORGOTH_PERMS.json` | ⬜ Todo | Initial permissions |

### Tests
| File | Status | Notes |
|---|---|---|
| `tests/conftest.py` | ⬜ Todo | |
| `tests/test_tools.py` | ⬜ Todo | |

---

## Phase 2 — Self-Modification Engine (morgoth/)

> Permissions are now enabled in `MORGOTH_PERMS.json`.
> Deliverables below are implemented and awaiting human review.

| File | Status | Notes |
|---|---|---|
| `ecosystem.config.js` | ✅ Done | PM2 app definition; interpreter=none, 2G memory cap, timestamped logs to data/logs/, PYTHONUNBUFFERED=1 |
| `scripts/setup_pm2.sh` | ✅ Done | Idempotent: installs pm2 if absent, deletes+restarts from ecosystem.config.js, saves, tails 50 lines, prints cheatsheet |
| `scripts/health_monitor.py` | ✅ Done | 60-second checks for Ollama, PostgreSQL, ChromaDB, persistent agents, memory, and stalled tasks |
| `scripts/init_db.py` | ✅ Done | Adds `objectives` and `ui_widgets` tables after base schema initialization |
| `core/objectives.py` | ✅ Done | Objective persistence, status updates, heuristic + LLM-assisted generation |
| `self_modify/code_writer.py` | ✅ Done | Permission-gated Python generation with syntax validation |
| `self_modify/code_tester.py` | ✅ Done | Isolated pytest runner with timeout and bounded test targets |
| `self_modify/diff_logger.py` | ✅ Done | Git diff capture and persistence to `self_modifications` |
| `self_modify/updater.py` | ✅ Done | Atomic integration, rollback on failed tests, diff logging |

### Verification

| Check | Status | Notes |
|---|---|---|
| `./.venv/bin/python -m pytest -q tests/test_phase2.py` | ✅ | 3 tests passed |
| `./.venv/bin/python -m compileall core/objectives.py self_modify scripts/health_monitor.py` | ✅ | All new Python modules compile |
| `./.venv/bin/python main.py` | ⚠️ Partial | Reached uvicorn startup and entered application startup without immediate traceback; full READY state not observed in short timeout window |

### PM2 Service Verification (2026-05-11)

| Check | Status | Notes |
|---|---|---|
| `bash scripts/setup_pm2.sh` | ✅ | pm2 installed via npm; morgoth started from ecosystem.config.js; logs tailed for 60s |
| `pm2 status` shows morgoth online | ✅ | PID 1742, 3m uptime, 0 restarts, 178.9 MB RAM |
| `curl http://localhost:8000/api/brain/status` → 200 | ✅ | Returned 200 immediately after startup |
| `pm2 logs morgoth \| grep -i "autonomous cycle"` | ✅ | "Autonomous cycle scheduled" present in logs |
| `pkill -f "main.py"; sleep 10; curl …/api/brain/status` → 200 | ✅ | PM2 restarted to PID 2433 (↺=1); 200 returned after 20s startup |

---

## PM2 Operations

Morgoth runs as a persistent PM2 service. Start with `bash scripts/setup_pm2.sh` on a fresh machine.

```
pm2 status              # check state
pm2 logs morgoth        # tail logs
pm2 restart morgoth     # restart
pm2 stop morgoth        # stop
pm2 monit               # live dashboard
pm2 startup             # configure auto-start on OS boot
```

Log files:
- Combined: `data/logs/pm2-combined.log`
- stdout:   `data/logs/pm2-out.log`
- stderr:   `data/logs/pm2-error.log`

---

## Phase 3 — Frontend (morgoth_ui/)

> Start Phase 3 only after Phase 1 backend is running and WebSocket is tested.
> Full spec in morgoth_ui/SPEC_UI.md

| Area | Status | Notes |
|---|---|---|
| Design system + Tailwind config | 🔒 Locked | Phase 3 |
| TypeScript types | 🔒 Locked | Phase 3 |
| WebSocket client | 🔒 Locked | Phase 3 |
| Zustand stores | 🔒 Locked | Phase 3 |
| Layout components | 🔒 Locked | Phase 3 |
| Dashboard page | 🔒 Locked | Phase 3 |
| Chat page | 🔒 Locked | Phase 3 |
| Agents page | 🔒 Locked | Phase 3 |
| Market page | 🔒 Locked | Phase 3 |
| Brain page | 🔒 Locked | Phase 3 |
| Admin page | 🔒 Locked | Phase 3 |

---

## Phase 2b — UI Refactor Backend Support

| File | Status | Notes |
|---|---|---|
| `api/routes/consciousness.py` | ✅ Done | Added recent THOUGHT aggregation for clusters and concept co-occurrence graph |
| `api/routes/objectives.py` | ✅ Done | Added objective list, create, and status-update endpoints backed by `core/objectives.py` |
| `api/routes/evolution.py` | ✅ Done | Added growth metrics and cumulative timeline endpoints from PostgreSQL tables |
| `api/server.py` | ✅ Done | Registered the new consciousness, objectives, and evolution routers |
| `tests/test_phase2b_routes.py` | ✅ Done | Added targeted tests for cluster/concept/timeline aggregation helpers |

---

## Phase 3 — Intelligence Expansion

| File | Status | Notes |
|---|---|---|
| `agents/research_agent.py` | ✅ Done | Deep research and synthesis agent with timeout degradation and UI task metadata |
| `agents/sentiment_agent.py` | ✅ Done | News and social sentiment analysis agent with timeout degradation and UI task metadata |
| `agents/macro_agent.py` | ✅ Done | Macro economic indicator agent backed by FRED tool context and UI task metadata |
| `tools/connectors/fred.py` | ✅ Done | FRED series search and observations tools using `FRED_API_KEY` from environment |
| `tools/connectors/reddit.py` | ✅ Done | Reddit public search and subreddit post tools |
| `tools/analysis/technical.py` | ✅ Done | Local RSI, MACD, SMA, and EMA analysis tool |
| `agents/agent_manager.py` | ✅ Done | Registers research, sentiment, and macro specialist agents without touching `core/` |
| `api/server.py` | ✅ Done | Registers Phase 3 FRED, Reddit, and technical-analysis tools in `build_tool_router()` |
| `tests/test_phase3_intelligence.py` | ✅ Done | Focused tests for specialist agent selection, FRED, Reddit, and TA tools |
| `core/brain.py` | ✅ Done | Adds bounded Morgoth identity, capped recall summaries, and an 8-tool chat schema subset before LLM calls |
| `tests/test_agents_and_llm.py` | ✅ Done | Covers system prompt injection, capped pre-chat recall context, chat tool subset selection, Ollama fallback, and non-fatal chat tool failures |

### Verification

| Check | Status | Notes |
|---|---|---|
| `./.venv/bin/python -m pytest -q tests/test_phase3_intelligence.py` | ✅ | 8 passed |
| `./.venv/bin/python -m compileall agents/research_agent.py agents/sentiment_agent.py agents/macro_agent.py agents/agent_manager.py tools/connectors/fred.py tools/connectors/reddit.py tools/analysis/technical.py tests/test_phase3_intelligence.py` | ✅ | All Phase 3 backend modules compile |
| `python3 -m compileall core/tool_router.py` | ✅ | Tool router module compiles after registration pass; literal `python` executable is not present on this machine |
| `./.venv/bin/python -m pytest -q tests/test_agents_and_llm.py` | ✅ | 5 passed after Ollama serialization/bootstrap race fix |
| `./.venv/bin/python -m compileall core/llm_client.py core/brain.py tests/test_agents_and_llm.py` | ✅ | Touched backend files compile |
| `./.venv/bin/python main.py` + chat curl | ✅ | Startup completed with no startup 500s; `POST /api/chat` returned `200` and identified Morgoth as Morgoth. Literal `python` executable is not present on this machine |

---

## Bootstrap Checklist

> To be completed by human before declaring Morgoth OPERATIONAL.

| Check | Status |
|---|---|
| Ollama reachable at OLLAMA_BASE_URL | ⬜ |
| deepseek-r1:14b model available | ⬜ |
| llama3:8b model available | ⬜ |
| PostgreSQL connection successful | ⬜ |
| All DB tables created by init_db.py | ⬜ |
| ChromaDB collections initialized | ⬜ |
| All 11 Layer 1 tools return success | ⬜ |
| FastAPI server starts on port 8000 | ⬜ |
| WebSocket /ws/chat accepts connection | ⬜ |
| Telegram notification received | ⬜ |
| Exploration report generated | ⬜ |
| First conversation with Morgoth successful | ⬜ |

---

## Decisions Made

> Log of implementation choices made when spec was ambiguous.

| Date | Decision | Reason |
|---|---|---|
| 2026-04-14 | `ui_widgets` table created during Phase 2 instead of waiting for Phase 5 | Root-level instructions required DB schema parity when introducing new tables |
| 2026-04-14 | `SafeUpdater` rolls back immediately on failed pytest validation | Robustness-first requirement forbids leaving partially integrated code in place |
| 2026-04-14 | Health monitor escalates repeated failures into monitoring objectives | Repeated faults should become tracked autonomous work, not only alerts |
| 2026-04-15 | `GetCryptoPriceTool` now serves per-symbol cached prices for 60 seconds and reuses stale cache on CoinGecko 429s | The UI poll cadence was exhausting CoinGecko limits and the market route needed a stable last-known-data fallback |
| 2026-04-15 | Agent creation accepts an optional `model` override | The API contract needed to accept the provided creation payload and return the actual model the UI should display |
| 2026-04-16 | Consciousness topics are derived from recent THOUGHT log tokens instead of a stored topic field | Existing logs do not persist explicit topic metadata, so Step 1 aggregates from content while keeping the API contract stable |
| 2026-04-17 | Ollama chat payloads now omit empty message fields and strip unsupported JSON Schema keywords from tool definitions | The local Ollama integration was rejecting tool-enabled requests with HTTP 400, and the safest contract is the minimal supported payload |
| 2026-04-25 | Phase 3 tools were implemented but not registered in `core/tool_router.py` | The session hard rule forbids touching `core/`; runtime registration needs human approval or a later task that allows that file |
| 2026-04-28 | Phase 3 tools are registered in `api/server.py` bootstrap | The existing runtime registration block is `build_tool_router()`, while `core/tool_router.py` only defines the registry class |
| 2026-05-11 | `tool_choice: required` not passed — Ollama API does not support it | The Ollama `/api/chat` endpoint accepts model-inference `options` (temperature etc.) but has no `tool_choice` parameter; the imperative prompt alone is sufficient |
| 2026-05-11 | `update_objective` uses dynamic SQL parameter building instead of a single fixed query | The method must handle any combination of status-only, evidence-only, or both; a single query with NULLable params would silently overwrite existing evidence with NULL |
| 2026-05-11 | Autonomous cycle stores objectives in ChromaDB, not PostgreSQL | `remember` tool writes to ChromaDB; `/api/objectives` reads PostgreSQL `objectives` table; bridging these is a future task |
| 2026-05-11 | `create_objective` uses hardcoded category='research' | `ObjectiveCategory` enum only has research/capability/monitoring/optimization; 'autonomous' caused a 500 on list endpoint; 'research' is the most appropriate default for autonomous knowledge-gap objectives |
| 2026-05-11 | process_message() converted from single-round to 5-round agentic tool loop | Single round prevented the model from calling create_objective after seeing news/price data; the while loop passes tools on every subsequent call so the model can chain discoveries into objective creation |
| 2026-05-12 | Force-completion happens in code (not prompt) after N cycles | llama3.1:8b reliably fails to call update_objective on its own after 6+ cycles; prompt-only fixes are insufficient for weak models; cycle_count column is idempotent via IF NOT EXISTS guard |
| 2026-05-12 | ALTER TABLE objectives ADD COLUMN IF NOT EXISTS cycle_count runs in PersistentMemory.initialize() with try/except | The objectives table is created by scripts/init_db.py separately; wrapping the migration in try/except keeps startup non-fatal on first boot before init_db.py has run |

---

## Dependency Changes

> Any packages added beyond the baseline requirements.txt in SPEC.md.

| Package | Version | Reason | Added by |
|---|---|---|---|
| — | — | — | — |

---

## Issues & Blockers

> Current known issues or blockers.

| Issue | Status | Notes |
|---|---|---|
| Task `result` loaded from PostgreSQL as string `'null'` caused `Task` validation failure on startup | ✅ Resolved | Fixed task row normalization to deserialize JSON text and coerce `'null'` to `None` before building `Task` models |
| CoinGecko `429 Too Many Requests` errors from `/api/market/prices` | ✅ Resolved | `GetCryptoPriceTool` now caches the last successful result per symbol for 60 seconds and returns cached data when CoinGecko rate-limits; `/api/market/prices` also catches `httpx.HTTPStatusError` and serves last known values |
| Chat POST/UI payload mismatch and duplicate live-response risk | ✅ Resolved | UI chat sender now includes `user_id`, uses the websocket when connected, and falls back to REST only when needed; backend POST contract remains `{content, user_id}` returning `BrainResponse` |
| Agent creation payload/response drift | ✅ Resolved | `POST /api/agents` now accepts optional `model` and returns the created agent shape the UI expects; the UI inserts the returned agent immediately instead of waiting for the next poll |
| `GET /api/agents` raised `PydanticSerializationError` because runtime agent objects leaked `OllamaLLMClient` into API payloads | ✅ Resolved | `BaseAgent.to_dict()` now returns an explicit serializable DTO and no longer serializes subclass runtime dependencies |
| Ollama `/api/chat` returned `400 Bad Request` for tool-enabled chat payloads | ✅ Resolved | Chat messages now serialize only supported fields, tool schemas are sanitized before dispatch, the exact payload is logged before send, and `Brain.process_message()` retries once without tools to isolate and bypass tool-schema rejections |
| Morgoth had no stable identity prompt and did not preload previous conversation memories before chat | ✅ Resolved | `Brain.process_message()` now starts every LLM turn with `SYSTEM_PROMPT`, calls the `recall` tool against the `conversations` collection before the LLM request, and injects returned memories as system context |
| Ollama `llama runner process has terminated` from chat context-window overflow | ✅ Resolved | `Brain.process_message()` now sends a sub-100-word system prompt, summarizes at most 3 recalled conversation memories using only `content[:200]`, and exposes only 8 common chat tools to the LLM while keeping all tools registered for direct execution |
| Concurrent Ollama runner crash from bootstrap self-test agent and chat race | ✅ Resolved | All `OllamaLLMClient.chat()` calls now share a module-level `asyncio.Lock`, awakening uses a direct Ollama ping instead of creating `self_test_agent`, and chat tool failures are returned to the model instead of surfacing as HTTP 500s |
| `python main.py` startup verification on this machine | ✅ Resolved | `./.venv/bin/python main.py` completed application startup and exposed port `8000`; the literal `python` executable is not installed in this shell |
| Live verification of chat response | ✅ Resolved | `curl -X POST http://localhost:8000/api/chat ...` returned `200 OK` with Morgoth identifying itself as Morgoth |
| Ollama runner crashes on first post-awakening chat request (`llama runner process has terminated: %!w(<nil>)`) | ✅ Resolved | `Brain.awaken()` now sends a full-context warmup chat (system prompt + 8 chat tools) immediately after the direct ping, so the model is loaded into VRAM with the same inference shape it will see in production before any real request arrives. Warmup is non-fatal. |

---

## Session Log

> Brief log of each work session for continuity.

| Date | Who | What was done |
|---|---|---|
| Project init | Human | Repos created, SPEC.md written, environment set up |
| 2026-04-14 | Codex | Implemented Phase 2 deliverables and added focused tests for self-modify components |
| 2026-04-15 | Codex | Added CoinGecko caching/rate-limit fallback, aligned chat and agent creation API contracts, and verified targeted backend tests plus Python compilation for touched modules |
| 2026-04-16 | Codex | Completed Phase 2b Step 1 by adding consciousness, objectives, and evolution endpoints plus focused backend aggregation tests |
| 2026-04-17 | Codex | Fixed the `/api/agents` serialization boundary, hardened Ollama chat payload construction and fallback behavior, added focused regression tests in `tests/test_agents_and_llm.py`, and confirmed targeted tests/compilation pass; live port-8000 verification stayed blocked by startup |
| 2026-04-25 | Codex | Completed Phase 3 backend Steps 1-6 and specialist agent registration: added research, sentiment, and macro agents; FRED, Reddit, and technical-analysis tools; and focused tests passing without live network calls |
| 2026-04-28 | Codex | Registered Phase 3 FRED, Reddit, and technical-analysis tools in the runtime tool router bootstrap and verified `core/tool_router.py` compilation with `python3` and the project venv |
| 2026-05-02 | Codex | Added Morgoth system identity prompt plus recall-backed conversation context in `core/brain.py`; updated focused regression coverage and verified tests/compilation |
| 2026-05-04 | Codex | Fixed Ollama chat context overflow by shortening `SYSTEM_PROMPT`, summarizing recalled memories, limiting LLM-visible chat tools to 8, and verifying focused tests/compilation; live curl remains blocked by local app startup not binding port 8000 |
| 2026-05-10 | Human | Full live end-to-end verification: startup OK, datetime chat OK (web_search called, 200 returned), ETH price OK (CoinGecko 2360.24 EUR +1.11%), brain logs OK, objectives empty. Ollama runner crashed on first chat attempt then self-recovered without restart. No code changes. |
| 2026-05-11 | Codex | Added post-awakening Ollama warmup in `Brain.awaken()` (full chat context + 8 tools, non-fatal); added `Brain.run_autonomous_cycle()` background loop driven by `AUTONOMOUS_CYCLE_MINUTES` config; started loop as asyncio task in `Brain.initialize()`; added `PersistentMemory.get_objectives()` for pending-objective queries. 34 tests pass; all three modules compile. |
| 2026-05-11 | Codex | Live verification: warmup PASS (both log lines present, ~1.7s), first chat PASS (200 OK, no Ollama crash), 1st autonomous cycle PASS (fired at ~58s after startup), 2nd autonomous cycle PASS (count reached 2). Objective creation: NO — model replied without calling tools (tool_calls=0); response stored in conversations episodic memory only. `.env` restored to AUTONOMOUS_CYCLE_MINUTES=10. |
| 2026-05-11 | Codex | Rewrote autonomous cycle prompts to be imperative ("ACT NOW. Tool calls only."). Verified: both cycles fired tool_calls=3 (get_news + get_crypto_price + remember). Note: `tool_choice: required` is not supported by Ollama API — prompt-only fix is sufficient. Objectives still show empty in `/api/objectives` because `remember` writes to ChromaDB while that endpoint reads PostgreSQL `objectives` table (architectural gap, not a regression). `.env` restored to AUTONOMOUS_CYCLE_MINUTES=10. |
| 2026-05-11 | Codex | Added `CreateObjectiveTool` (tools/objectives_tool.py) writing to PostgreSQL `objectives` table with valid category='research'; added `create_objective()` to `PersistentMemory`; registered tool in api/server.py; added to CHAT_TOOL_NAMES; updated autonomous cycle prompt to call `create_objective` in STEP 3; converted tool execution from single-round `if` to 5-round agentic `while` loop in `process_message()` so model can chain tool calls across rounds. Live result: objective "Crypto Market Sentiment Analysis" confirmed in PostgreSQL at 09:58:26; `/api/objectives` returns 200 with data. 39 tests pass. `.env` restored to AUTONOMOUS_CYCLE_MINUTES=10. |
| 2026-05-11 | Codex | Created `ecosystem.config.js` (PM2 app definition with interpreter=none, autorestart, 2G cap, timestamped logs); rewrote `scripts/setup_pm2.sh` to be fully idempotent (installs pm2 if absent, delete+start from ecosystem config, save, tail, cheatsheet); chmod +x applied. All 5 verification checks passed: script clean, pm2 status online, brain/status 200, autonomous cycle in logs, survived pkill+restart with 200 returned. |
| 2026-05-11 | Codex | Fixed infinite reddit_search loop: (1) Added `UpdateObjectiveTool` (`tools/objectives_tool.py`) writing status+evidence to PostgreSQL; added `PersistentMemory.update_objective()` (`memory/persistent.py`); registered in `api/server.py`; added to `CHAT_TOOL_NAMES` in `core/brain.py`. (2) Rewrote autonomous cycle objective branch to recall past actions from episodic memory and prompt Morgoth to decide done/not-done explicitly. (3) Added progression rule to SYSTEM_PROMPT. 42 tests pass; all 5 touched modules compile. |
| 2026-05-12 | Codex | Force-completion via cycle cap: (1) `ALTER TABLE objectives ADD COLUMN IF NOT EXISTS cycle_count INTEGER DEFAULT 0` in `PersistentMemory.initialize()`. (2) `increment_cycle_count()` method in `memory/persistent.py`. (3) Auto-complete logic at cycle start in `Brain.run_autonomous_cycle()` — increments counter, force-marks objective done after `MAX_CYCLES_PER_OBJECTIVE` cycles, returns early to unblock the loop. (4) `MAX_CYCLES_PER_OBJECTIVE` field in `AppConfig` + env loading in `load_config()`. (5) Added `MAX_CYCLES_PER_OBJECTIVE=3` to `.env`. (6) More directive no-objectives prompt pointing to `create_objective`. (7) Two new unit tests for `increment_cycle_count`. 44 tests pass; all touched modules compile and import cleanly. |
