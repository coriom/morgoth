# AGENTS.md — Codex Behavior Instructions for Morgoth

> This file tells Codex how to work on this repository.
> Read this file AND SPEC.md before touching any code.

---

## Identity & Context

You are building Morgoth, an autonomous Python intelligence system.
The full specification lives in `SPEC.md` at the root of this repository.
SPEC.md is your single source of truth. When in doubt, refer to it.

---

## How to Work

### Always do this first
1. Read `SPEC.md` completely before writing any file
2. Read `PROGRESS.md` to know what is already done and what is next
3. Update `PROGRESS.md` after completing each file or group of files

### File generation order
Follow the Phase 1 order in SPEC.md section 15 exactly.
Do not skip ahead. Do not generate files out of order.
Each file may depend on the previous one.

### After generating each file
- Verify it respects the interface contract in SPEC.md section 4
- Verify it loads secrets from `.env` only, never hardcoded
- Verify it uses async/await throughout
- Update PROGRESS.md to mark it as done

---

## Code Rules

- **Language**: Python 3.11+ only
- **Async**: use `async/await` everywhere — zero blocking calls
- **Models**: use `pydantic` for all data models and validation
- **Logging**: use `loguru` — never use `print()` for logging
- **Type hints**: required on every function signature
- **Docstrings**: required on every public method and class
- **Secrets**: always loaded from `.env` via `python-dotenv` — never hardcoded
- **Imports**: use absolute imports from project root

## Naming Conventions
- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Async functions: prefix with `async def`, no special naming needed

---

## Architecture Rules

### Zone permissions — CRITICAL
Never modify files in the IMMUTABLE ZONE:
- `core/`
- `api/`
- `memory/working.py`, `memory/episodic.py`, `memory/persistent.py`
- `self_modify/updater.py`, `self_modify/code_tester.py`
- `SPEC.md`, `AGENTS.md`, `PROGRESS.md`, `MORGOTH_PERMS.json`, `.env`

Only generate or modify files in the EVOLVABLE ZONE:
- `tools/` (except `base_tool.py` once created)
- `agents/` (except `base_agent.py` once created)
- `data/`
- `tests/`
- `notifications/`

### Interface contracts — CRITICAL
Every Tool must inherit from `BaseTool` and implement the contract in SPEC.md section 4.1.
Every Agent must inherit from `BaseAgent` and implement the contract in SPEC.md section 4.2.
Every Task must follow the schema in SPEC.md section 4.3.
WebSocket messages must follow the schema in SPEC.md section 4.4.
Log entries must follow the schema in SPEC.md section 4.5.
Never break these contracts.

### Database
- PostgreSQL via `asyncpg` — connection string from `POSTGRES_URL` env var
- ChromaDB local persistent store in `data/chroma_db/`
- Never use SQLite
- All tables defined in SPEC.md section 5.1 — follow the schema exactly
- Always include `user_id` field in every table for future multi-user support

---

## What NOT to do

- Never hardcode API keys, URLs, passwords, or model names
- Never use `time.sleep()` — use `asyncio.sleep()`
- Never use `requests` — use `httpx` with async client
- Never write to files outside the EVOLVABLE ZONE
- Never modify `MORGOTH_PERMS.json` — only read it
- Never generate a `.env` file — it is created manually by the human
- Never commit secrets
- Never use `print()` for logging — use `loguru`
- Never skip type hints
- Never generate the `self_modify` tools until explicitly asked (Phase 2)

---

## Dependencies

Use only the packages listed in `requirements.txt` in SPEC.md section 15.
If you need an additional package not listed there, add it to `requirements.txt`
and note it in `PROGRESS.md` under "Dependency changes".

---

## Testing

Every tool must have at least one unit test in `tests/`.
Tests go in `tests/test_<module_name>.py`.
Use `pytest` and `pytest-asyncio`.
Mock external API calls in tests — never make real HTTP calls in tests.

---

## When You Are Stuck

If the spec is ambiguous on a point, choose the more conservative option
(less permissions, more validation, more logging) and note your choice in PROGRESS.md
under "Decisions made".

---

## Progress Tracking

After completing each deliverable, update `PROGRESS.md`:
- Mark the file as ✅ Done
- Add any notes about implementation choices
- List any blockers or follow-up items