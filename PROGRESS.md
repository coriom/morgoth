# PROGRESS.md — Morgoth Development Tracker

> Updated by Codex after each completed deliverable.
> Updated by human after each review, test, or decision.

---

## Current Status

**Phase**: 1 — Backend Bootstrap  
**Overall**: 0 / 31 files complete  
**Last updated**: project initialized  
**Next action**: Begin Phase 1 — start with `core/config.py`

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

> Start Phase 2 only after Phase 1 is fully tested and operational.
> Human must manually set `can_self_modify: true` in MORGOTH_PERMS.json to unlock.

| File | Status | Notes |
|---|---|---|
| `self_modify/code_writer.py` | 🔒 Locked | Phase 2 |
| `self_modify/code_tester.py` | 🔒 Locked | Phase 2 |
| `self_modify/diff_logger.py` | 🔒 Locked | Phase 2 |
| `self_modify/updater.py` | 🔒 Locked | Phase 2 |
| `agents/research_agent.py` | 🔒 Locked | Phase 2 |
| `agents/crypto_agent.py` | 🔒 Locked | Phase 2 |

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
| — | — | — |

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

---

## Session Log

> Brief log of each work session for continuity.

| Date | Who | What was done |
|---|---|---|
| Project init | Human | Repos created, SPEC.md written, environment set up |
