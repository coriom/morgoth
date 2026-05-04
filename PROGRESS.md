# PROGRESS.md — Morgoth Development Tracker

> Updated by Codex after each completed deliverable.
> Updated by human after each review, test, or decision.

---

## Current Status

**Phase**: 3 — Intelligence Expansion  
**Overall**: Phase 1 stable, Phase 2 implemented, Phase 2b backend endpoints complete, Phase 3 Steps 1-6 plus agent and tool-router registration complete  
**Last updated**: 2026-05-04 by Codex — Brain chat payload now caps memory context and LLM tool schemas to avoid Ollama context overflow
**Next action**: Human review Ollama context-window overflow fix

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
| `scripts/setup_pm2.sh` | ✅ Done | Idempotent PM2 bootstrap with restart delay and persisted logs |
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
| `tests/test_agents_and_llm.py` | ✅ Done | Covers system prompt injection, capped pre-chat recall context, chat tool subset selection, and Ollama fallback |

### Verification

| Check | Status | Notes |
|---|---|---|
| `./.venv/bin/python -m pytest -q tests/test_phase3_intelligence.py` | ✅ | 8 passed |
| `./.venv/bin/python -m compileall agents/research_agent.py agents/sentiment_agent.py agents/macro_agent.py agents/agent_manager.py tools/connectors/fred.py tools/connectors/reddit.py tools/analysis/technical.py tests/test_phase3_intelligence.py` | ✅ | All Phase 3 backend modules compile |
| `python3 -m compileall core/tool_router.py` | ✅ | Tool router module compiles after registration pass; literal `python` executable is not present on this machine |
| `./.venv/bin/python -m pytest -q tests/test_agents_and_llm.py` | ✅ | 4 passed after context-window overflow fix |
| `./.venv/bin/python -m compileall core/brain.py tests/test_agents_and_llm.py` | ✅ | Touched backend files compile |
| `./.venv/bin/python main.py` + chat curl | ⚠️ Blocked | Reached `Waiting for application startup.` but did not bind `8000` within the local verification window; literal `python` executable is not present on this machine |

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
| `python main.py` startup verification on this machine | ⚠️ Partial | The process reached `Waiting for application startup.` but did not bind port `8000` within the local timeout window, so live curl verification against the full app remains blocked by startup duration/dependencies |
| Live verification of `curl http://localhost:8000/api/agents` and websocket chat response | ⚠️ Blocked by environment | After the fixes, `main.py` still stops at `Waiting for application startup.` on 2026-04-17 and never exposes port `8000`, so the requested end-to-end checks could not be completed in this session |

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
