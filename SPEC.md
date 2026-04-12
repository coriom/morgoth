# SPEC.md — Morgoth Autonomous Intelligence System

On interagit entre nous en Français mais voici les spec en anglais : 

> Version 1.0 — Bootstrap Specification  
> This document is the single source of truth for Codex during Phase 1 and Phase 2 development.  
> Morgoth itself must read and respect this document at all times.

---

## 0. Philosophy & Principles

- **Python-first** — all backend logic is Python 3.11+. No exceptions.
- **Local-first** — Morgoth runs entirely on local hardware (Ollama). Cloud APIs are tools, not dependencies.
- **Modular by contract** — modules communicate through defined interfaces. Internal implementation can change freely.
- **Evolvable by design** — the file structure defined here is a starting point, not a contract. Morgoth may reorganize, split, merge, or create modules within the EVOLVABLE ZONE freely.
- **Security by layers** — every capability Morgoth gains must pass through a permission check. No capability is assumed.
- **Scalability by default** — every schema, every interface, every data model must anticipate growth. Add `user_id` fields even if unused. Use queues even for single tasks.

---

## 1. Hardware & Runtime Context

| Property | Value |
|---|---|
| GPU | NVIDIA RTX 3060 12GB VRAM |
| LLM Runtime | Ollama (local) |
| Primary Model | `deepseek-r1:14b-qwen-distill-q4_K_M` |
| Agent Model | `llama3.1:8b` |
| Max Concurrent Agents | 3 (VRAM constraint — enforced in code) |
| Python Version | 3.11+ |
| OS Target | Linux / Windows WSL2 |

### Model Selection Logic
```
IF task_type IN ["reasoning", "finance", "code_review", "self_modify"]:
    use deepseek-r1:14b
ELIF task_type IN ["quick_lookup", "agent_subtask", "summarize"]:
    use llama3.1:8b
```

---

## 2. Repository Structure

Two separate repositories:

```
morgoth/          ← Python backend (this spec)
morgoth_ui/       ← Next.js frontend (separate spec)
```

### morgoth/ — Full Structure

```
morgoth/
├── core/                        🔒 IMMUTABLE
│   ├── brain.py                 # Main orchestration loop
│   ├── llm_client.py            # Ollama interface
│   ├── tool_router.py           # Routes tool calls to implementations
│   ├── scheduler.py             # Task queue manager
│   └── config.py                # Global config loader
│
├── memory/                      🔒 IMMUTABLE (engines) / 🔓 EVOLVABLE (schemas)
│   ├── working.py               # In-context short-term memory
│   ├── episodic.py              # ChromaDB vector store
│   └── persistent.py           # PostgreSQL structured knowledge
│
├── tools/                       🔓 EVOLVABLE
│   ├── base_tool.py             # Abstract Tool class (interface contract)
│   ├── web_search.py
│   ├── code_executor.py         # Sandboxed Python execution
│   ├── file_manager.py
│   └── data_feeds/
│       ├── crypto.py            # CoinGecko API
│       ├── finance.py           # Yahoo Finance / FRED
│       └── news.py              # RSS + web scraping
│
├── agents/                      🔓 EVOLVABLE
│   ├── base_agent.py            # Abstract Agent class (interface contract)
│   ├── agent_manager.py         # Lifecycle management
│   ├── research_agent.py
│   └── crypto_agent.py
│
├── self_modify/                 🔒 IMMUTABLE (engine) / 🔓 EVOLVABLE (targets)
│   ├── code_writer.py           # Generates new Python modules
│   ├── code_tester.py           # Runs pytest on generated code
│   ├── updater.py               # Integrates validated code
│   └── diff_logger.py           # Logs all self-modifications
│
├── notifications/               🔓 EVOLVABLE
│   └── telegram.py              # Telegram bot notifier
│
├── api/                         🔒 IMMUTABLE
│   ├── server.py                # FastAPI app entry
│   ├── routes/
│   │   ├── chat.py              # POST /chat, WS /ws/chat
│   │   ├── agents.py            # CRUD agent management
│   │   ├── market.py            # Market data endpoints
│   │   ├── brain.py             # Brain status + logs
│   │   └── admin.py             # Permissions, config
│   └── ws/
│       └── handler.py           # WebSocket connection manager
│
├── data/                        🔓 EVOLVABLE
│   ├── chroma_db/               # ChromaDB persistent store
│   └── logs/                    # Structured JSON logs
│
├── tests/                       🔓 EVOLVABLE
│   ├── conftest.py
│   └── test_tools.py
│
├── scripts/
│   └── init_db.py               # DB initialization script
│
├── SPEC.md                      🔒 READ-ONLY for Morgoth
├── MORGOTH_PERMS.json           🔒 Human-editable only
├── .env                         # Secrets — never logged, never in LLM context
├── requirements.txt
└── main.py                      # Entry point
```

---

## 3. Zone Permissions

```
🔒 IMMUTABLE ZONE
Files Morgoth can NEVER modify without explicit human approval:
- core/*
- memory/working.py, memory/episodic.py, memory/persistent.py
- self_modify/updater.py, self_modify/code_tester.py
- api/*
- SPEC.md
- MORGOTH_PERMS.json
- .env

🔓 EVOLVABLE ZONE
Files Morgoth may create, modify, split, merge freely:
- tools/* (except base_tool.py)
- agents/* (except base_agent.py)
- data_feeds/*
- notifications/*
- tests/*
- Any new directory Morgoth creates at root level
```

---

## 4. Interface Contracts

These interfaces are FIXED. Morgoth must never break them.

### 4.1 Tool Contract

Every tool in `tools/` must inherit from `BaseTool` and implement:

```python
from abc import ABC, abstractmethod
from typing import Any

class BaseTool(ABC):
    name: str                    # unique snake_case identifier
    description: str             # used by LLM to decide when to call this tool
    parameters: dict             # JSON Schema of input parameters

    @abstractmethod
    async def execute(self, **kwargs) -> dict:
        """
        Returns:
            {
                "success": bool,
                "result": Any,
                "error": str | None,
                "metadata": dict   # timing, tokens, source, etc.
            }
        """
        pass

    def to_ollama_schema(self) -> dict:
        """Returns OpenAI-compatible function calling schema for Ollama."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }
```

### 4.2 Agent Contract

Every agent in `agents/` must inherit from `BaseAgent`:

```python
from abc import ABC, abstractmethod
from enum import Enum

class AgentType(Enum):
    EPHEMERAL = "ephemeral"      # dies when task completes
    PERSISTENT = "persistent"    # runs until explicitly stopped

class AgentStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

class BaseAgent(ABC):
    agent_id: str                # uuid4
    name: str
    agent_type: AgentType
    status: AgentStatus
    model: str                   # which ollama model to use
    tools: list[str]             # list of tool names this agent can use
    created_at: datetime
    user_id: str                 # future multi-user support

    @abstractmethod
    async def run(self, task: str) -> dict: pass

    @abstractmethod
    async def pause(self) -> None: pass

    @abstractmethod
    async def stop(self) -> None: pass

    def to_dict(self) -> dict:
        """Serializable representation for API and logging."""
        pass
```

### 4.3 Task Contract

```python
from enum import Enum

class TaskPriority(Enum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    BACKGROUND = 3

class TaskType(Enum):
    ONE_SHOT = "one_shot"
    RECURRING = "recurring"
    TRIGGERED = "triggered"

class Task:
    task_id: str                 # uuid4
    type: TaskType
    priority: TaskPriority
    description: str
    agent_id: str | None         # assigned agent
    created_by: str              # "morgoth" or "human"
    created_at: datetime
    scheduled_at: datetime | None
    recurrence_cron: str | None  # cron string for RECURRING tasks
    status: str
    result: dict | None
    user_id: str
```

### 4.4 WebSocket Message Contract

All WebSocket messages between Morgoth and the UI follow this schema:

```typescript
// Outbound (Morgoth → UI)
{
  "type": "thought" | "action" | "result" | "error" | "agent_update" | "market_update" | "system",
  "timestamp": "ISO8601",
  "agent_id": "string | null",
  "content": "string",
  "metadata": {}
}

// Inbound (UI → Morgoth)
{
  "type": "chat" | "command",
  "content": "string",
  "user_id": "string"
}
```

### 4.5 Log Entry Contract

Every log entry written to disk and streamed to UI:

```python
{
    "timestamp": "ISO8601",
    "level": "THOUGHT" | "ACTION" | "RESULT" | "ERROR" | "SYSTEM",
    "agent": "morgoth_core | agent_id | system",
    "content": "string",
    "tokens_used": int | None,
    "duration_ms": int | None,
    "user_id": "string"
}
```

---

## 5. Data Layer

### 5.1 PostgreSQL (Persistent Knowledge)

Connection string loaded from `.env` as `POSTGRES_URL`.

**Tables to initialize on first run:**

```sql
-- Tasks
CREATE TABLE tasks (
    task_id UUID PRIMARY KEY,
    type VARCHAR(20),
    priority INTEGER,
    description TEXT,
    agent_id UUID,
    created_by VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    scheduled_at TIMESTAMPTZ,
    recurrence_cron VARCHAR(100),
    status VARCHAR(20),
    result JSONB,
    user_id VARCHAR(100) DEFAULT 'default'
);

-- Agents
CREATE TABLE agents (
    agent_id UUID PRIMARY KEY,
    name VARCHAR(100),
    agent_type VARCHAR(20),
    status VARCHAR(20),
    model VARCHAR(100),
    tools JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    stopped_at TIMESTAMPTZ,
    user_id VARCHAR(100) DEFAULT 'default'
);

-- Logs
CREATE TABLE logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    level VARCHAR(20),
    agent VARCHAR(100),
    content TEXT,
    tokens_used INTEGER,
    duration_ms INTEGER,
    user_id VARCHAR(100) DEFAULT 'default'
);

-- Knowledge facts
CREATE TABLE knowledge (
    fact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category VARCHAR(100),
    key VARCHAR(255),
    value TEXT,
    source VARCHAR(255),
    confidence FLOAT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    user_id VARCHAR(100) DEFAULT 'default'
);

-- Self-modification history
CREATE TABLE self_modifications (
    mod_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    file_path VARCHAR(500),
    diff TEXT,
    reason TEXT,
    test_result JSONB,
    approved_by VARCHAR(50),
    user_id VARCHAR(100) DEFAULT 'default'
);

-- Market snapshots
CREATE TABLE market_snapshots (
    snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    symbol VARCHAR(20),
    price FLOAT,
    change_24h FLOAT,
    volume_24h FLOAT,
    metadata JSONB
);
```

### 5.2 ChromaDB (Episodic Memory)

Collections:
- `conversations` — all chat history with embeddings
- `research` — all web searches + results
- `decisions` — Morgoth's decisions + outcomes
- `market_patterns` — observed market patterns
- `code_archive` — code Morgoth has written

Each document must include metadata: `{ "timestamp", "agent_id", "user_id", "category" }`

### 5.3 Environment Variables (.env)

```env
# Database
POSTGRES_URL=postgresql://user:password@localhost:5432/morgoth

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_PRIMARY_MODEL=deepseek-r1:14b-qwen-distill-q4_K_M
OLLAMA_AGENT_MODEL=llama3.1:8b

# APIs (free tier to start)
COINGECKO_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Security
SECRET_KEY=change_me_on_first_run
MAX_CONCURRENT_AGENTS=3

# Logging
LOG_RETENTION_DAYS=30
LOG_LEVEL_THOUGHT=true
```

---

## 6. API Specification (FastAPI — port 8000)

### REST Endpoints

```
POST   /api/chat                    # Send a message to Morgoth
GET    /api/chat/history            # Retrieve conversation history

GET    /api/agents                  # List all agents
POST   /api/agents                  # Create a new agent
GET    /api/agents/{agent_id}       # Get agent details
DELETE /api/agents/{agent_id}       # Stop and remove agent

GET    /api/market/prices           # Current prices (top cryptos + indices)
GET    /api/market/history/{symbol} # Price history for a symbol

GET    /api/brain/status            # LLM status, VRAM usage, model active
GET    /api/brain/logs              # Paginated log history
GET    /api/brain/tasks             # Task queue state

GET    /api/admin/permissions       # Read MORGOTH_PERMS.json
PATCH  /api/admin/permissions       # Update permissions (human only)
```

### WebSocket

```
WS /ws/chat      # Bidirectional chat + live event stream
```

WebSocket events streamed to UI:
- Every THOUGHT, ACTION, RESULT log entry in real time
- Agent status changes
- Market price updates (every 60 seconds)
- Self-modification notifications
- System alerts

### CORS
Allow `http://localhost:3000` during development.

---

## 7. Core Brain Loop

```python
# Pseudocode — brain.py main loop
async def run():
    await startup_checks()          # verify Ollama, DB, tools
    await load_memory()             # load recent context from ChromaDB
    await start_recurring_tasks()   # resume scheduled tasks from DB

    while True:
        # Process incoming messages (from WebSocket queue)
        if message := await message_queue.get():
            response = await process_message(message)
            await broadcast_to_ui(response)

        # Process task queue
        if task := await get_next_task():
            if agent_slots_available():
                await dispatch_task(task)

        await asyncio.sleep(0.1)
```

---

## 8. Tool Specifications (Layer 1)

Minimum tools required for Morgoth to be declared OPERATIONAL:

| Tool | Implementation | Free Tier |
|---|---|---|
| `web_search` | DuckDuckGo API (no key needed) | ✅ |
| `execute_python` | subprocess in temp dir, 30s timeout | ✅ |
| `read_file` | OS file read within allowed paths | ✅ |
| `write_file` | OS file write within EVOLVABLE ZONE only | ✅ |
| `get_crypto_price` | CoinGecko public API | ✅ |
| `get_crypto_history` | CoinGecko public API | ✅ |
| `get_news` | RSS feeds (no key needed) | ✅ |
| `create_agent` | Calls agent_manager.create() | ✅ |
| `notify` | Telegram (requires token) | Free |
| `remember` | Write to ChromaDB episodic memory | ✅ |
| `recall` | Query ChromaDB semantic search | ✅ |

`self_modify` tool is NOT included in Layer 1. It is added manually after bootstrap validation.

---

## 9. Self-Modification Engine

### Rules (non-negotiable)

1. Morgoth may only generate code targeting files in the EVOLVABLE ZONE.
2. Every generated file must pass `pytest tests/` before being written to disk.
3. Every modification is recorded in the `self_modifications` PostgreSQL table.
4. If tests fail, the modification is discarded and Morgoth logs the failure.
5. Modifications to any file that imports from `core/` require human approval (notification sent via Telegram).
6. Morgoth must include a `reason` string explaining every self-modification.

### Workflow

```
Morgoth identifies a gap or improvement
    → code_writer.py generates candidate code
    → code_tester.py runs pytest in isolated subprocess
    → IF tests pass:
        → diff_logger.py records the diff to DB
        → updater.py writes file to disk
        → Morgoth logs ACTION: "self_modified {file}"
    → IF tests fail:
        → discard candidate
        → log THOUGHT: "self_modification failed: {reason}"
        → optionally retry with different approach
```

---

## 10. Bootstrap Protocol

### Step 1 — AWAKENING (automatic on first `python main.py`)

Morgoth must:
- Verify Ollama is reachable at `OLLAMA_BASE_URL`
- Verify primary and agent models are pulled
- Verify PostgreSQL connection and run `init_db.py` if tables don't exist
- Verify ChromaDB collections exist (create if not)
- Test each Layer 1 tool (one call each, log result)
- Report status: `READY` or list what is missing

### Step 2 — BRIEFING (human → Morgoth, one time)

Human sends a structured onboarding message via the UI chat. This message is stored as a `FOUNDATIONAL` memory in ChromaDB that Morgoth cannot overwrite. Suggested format:

```
Name: Morgoth
Purpose: Autonomous intelligence system focused on finance, crypto, and general knowledge acquisition.
Owner: [your name]
Timezone: [your timezone]
Current permissions: see MORGOTH_PERMS.json
Initial focus: Monitor top 20 crypto by market cap. Track BTC, ETH, SOL daily. Research any topic asked.
```

### Step 3 — EXPLORATION (Morgoth autonomous, up to 24h)

Morgoth self-assigns a set of BACKGROUND tasks:
- Test all tools and log results
- Fetch and store initial market data
- Run 3 web searches on topics relevant to finance/crypto
- Write a self-assessment report to `data/exploration_report.md`

### Step 4 — OPERATIONAL

Declared when:
- All Layer 1 tools return `success: true`
- At least 1 recurring task is running (e.g., crypto price watcher)
- Exploration report has been generated
- WebSocket connection to UI is stable

Human activates `self_modify` permission manually in `MORGOTH_PERMS.json`.

---

## 11. MORGOTH_PERMS.json

```json
{
  "version": "1.0",
  "last_updated_by": "human",
  "permissions": {
    "can_create_ephemeral_agents": true,
    "can_create_persistent_agents": false,
    "can_self_modify": false,
    "can_store_secrets": false,
    "can_pull_ollama_models": false,
    "can_execute_code": true,
    "can_write_files": true,
    "can_send_notifications": true,
    "can_access_internet": true,
    "can_place_real_orders": false
  },
  "evolvable_zone_paths": [
    "tools/",
    "agents/",
    "data/",
    "tests/",
    "notifications/"
  ],
  "immutable_zone_paths": [
    "core/",
    "api/",
    "memory/working.py",
    "memory/episodic.py",
    "memory/persistent.py",
    "self_modify/updater.py",
    "self_modify/code_tester.py",
    "SPEC.md",
    "MORGOTH_PERMS.json",
    ".env"
  ],
  "notification_levels": {
    "INFO": ["ui"],
    "WARNING": ["ui", "log"],
    "CRITICAL": ["ui", "log", "telegram"]
  },
  "task_limits": {
    "max_concurrent_agents": 3,
    "max_recurring_tasks": 10
  }
}
```

---

## 12. Notification System

### Telegram

- One bot, one chat ID (owner only)
- Message format: `[MORGOTH/{level}] {content}`
- Rate limit: max 1 message per minute per level to avoid spam
- Types of events that trigger Telegram CRITICAL:
  - Any tool returning 5 consecutive errors
  - Self-modification attempted on immutable zone
  - Ollama connection lost
  - PostgreSQL connection lost
  - Agent crash

---

## 13. Logging

- All logs written as JSON lines to `data/logs/morgoth_YYYY-MM-DD.log`
- Also streamed in real time over WebSocket to UI
- Retention: 30 days (Morgoth runs a nightly BACKGROUND task to purge old files)
- `THOUGHT` level logs can be toggled via `LOG_LEVEL_THOUGHT` env var
- All logs also inserted into PostgreSQL `logs` table for queryability

---

## 14. Security Rules

These rules apply at all times regardless of `MORGOTH_PERMS.json`:

1. API keys and secrets are NEVER included in LLM prompts or stored in ChromaDB/logs.
2. Code execution is always in a subprocess with a 30-second timeout and no network access.
3. File writes are checked against `evolvable_zone_paths` before execution.
4. Morgoth never modifies `MORGOTH_PERMS.json` — it can only read it.
5. Morgoth never modifies `.env` — it may request the human add a key via Telegram notification.
6. Any attempt by Morgoth to access immutable zone paths for writing must raise a `PermissionDeniedError` and log a CRITICAL entry.

---

## 15. Codex Development Instructions

> This section is specifically for Codex reading this spec.

### Phase 1 Deliverables

Build the following in order:

1. `core/config.py` — loads `.env`, reads `MORGOTH_PERMS.json`
2. `core/llm_client.py` — async Ollama client with function calling support
3. `memory/episodic.py` — ChromaDB wrapper with the 5 collections
4. `memory/persistent.py` — asyncpg PostgreSQL client, creates tables on init
5. `tools/base_tool.py` — BaseTool abstract class
6. All 11 Layer 1 tools
7. `agents/base_agent.py` — BaseAgent abstract class
8. `agents/agent_manager.py` — lifecycle manager
9. `core/tool_router.py` — routes LLM tool calls to tool implementations
10. `core/scheduler.py` — task queue backed by PostgreSQL
11. `notifications/telegram.py` — Telegram notifier
12. `api/server.py` + all routes + WebSocket handler
13. `core/brain.py` — main orchestration loop
14. `main.py` — entry point with AWAKENING protocol
15. `scripts/init_db.py` — DB initialization
16. `tests/conftest.py` + `tests/test_tools.py`

### Phase 2 Deliverables

1. `self_modify/code_writer.py`
2. `self_modify/code_tester.py`
3. `self_modify/diff_logger.py`
4. `self_modify/updater.py`
5. `agents/research_agent.py`
6. `agents/crypto_agent.py`

### Code Style

- Use `async/await` throughout — no synchronous blocking calls
- Use `pydantic` for all data models
- Use `loguru` for internal logging (structured output)
- All functions must have type hints
- All public methods must have docstrings
- Every tool must have at least one unit test

### Dependencies (requirements.txt baseline)

```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
websockets>=12.0
asyncpg>=0.29.0
chromadb>=0.5.0
ollama>=0.2.0
pydantic>=2.7.0
loguru>=0.7.0
python-dotenv>=1.0.0
httpx>=0.27.0
feedparser>=6.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
apscheduler>=3.10.0
```

---

## 16. morgoth_ui/ — Frontend Spec Summary

> Full Next.js spec lives in `morgoth_ui/SPEC.md`. Summary for reference:

- **Framework**: Next.js 14 (App Router)
- **Styling**: Tailwind CSS + shadcn/ui
- **Charts**: Recharts
- **WebSocket**: native browser WebSocket to `ws://localhost:8000/ws/chat`
- **Pages**:
  - `/` — Main dashboard (market overview, brain status, recent logs)
  - `/chat` — Full-screen chat with Morgoth
  - `/agents` — Agent monitor (list, create, stop)
  - `/market` — Crypto + finance deep view
  - `/brain` — Logs, task queue, self-modification history
  - `/admin` — Permissions editor
- **Theme**: Dark mode only, terminal aesthetic

---

*End of SPEC.md v1.0*  
*Next revision triggered by: Phase 2 completion or Morgoth's first self-modification.*