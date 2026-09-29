# Internal LLM migration

**Blocked for inference:** on 2026-09-29 both direct probes emitted tool events
under CLI 0.159.0 despite the restrictions below. `SAFE_FOR_WORKLOADS=False`
therefore rejects every workload before invoking Codex. There is no environment
bypass. The automatic path falls to Ollama (or raises with fallback disabled).
A successful text response alone would not qualify the tool surface. A new
structural audit and successful canary are required before enabling this gate.

Codex is the default for THESIS, SYNTHESIS, reflect, shadow and reserved scout
routing; chat stays Ollama. This has no relation to the development executor.
No service restart is part of this migration. Human gate 3 remains human-only.

If existing runtime overrides select Claude, the operator must replace them:

```sh
export MORGOTH_LLM_THESIS=codex-cli:default
export MORGOTH_LLM_SYNTHESIS=codex-cli:default
export MORGOTH_LLM_REFLECT=codex-cli:default
export MORGOTH_LLM_SHADOW=codex-cli:default
export MORGOTH_LLM_SCOUT=codex-cli:default
export REFLECT_PROVIDER=codex-cli
export SHADOW_PROVIDER=codex-cli
```

The task-specific THESIS setting takes precedence over legacy THESIS_GENERATOR.
Production `.env` was neither inspected nor changed. Existing running processes
retain their configuration; activation is a later operator decision.
`morgoth reflect --provider codex-cli` selects the new engine explicitly.
Rollback is explicit: use `claude-cli:default` in the task overrides above,
`REFLECT_PROVIDER=claude-cli`, `SHADOW_PROVIDER=claude-cli`, or
`morgoth reflect --provider claude-cli`. No reflection is required for verification.

The descending fallback is API → Codex → Ollama; explicitly selected Claude
can fall to Ollama, but nothing automatically selects Claude. The existing
LLM_FALLBACK_ENABLED switch, event recording and local bottom rung are retained.
No paid API is selected by default; OPENAI_API_KEY is neither used nor inherited.
Heartbeat checks binary/version only, not login, quota or inference readiness.

## Restricted transport

Reviewed CLI: `codex-cli 0.159.0`. Unknown versions or missing mandatory flags /
feature disables fail closed. `core/llm/codex_cli.py` is the exact argv source:

```text
codex exec --skip-git-repo-check --ephemeral --ignore-user-config --ignore-rules
  --sandbox read-only --enable skip_host_skill_discovery --json --color never
  --output-last-message <fresh-temp-dir>/final.txt
  [--disable <each DISABLED feature>]
  [-c <each CONFIG assignment>] [--model <non-default model>] -
```

Prompt travels on stdin, cwd is a fresh `/tmp` directory. Environment allowlist:
HOME, PATH, CODEX_HOME, LANG; no inherited API keys, executor state or Node hooks.
Web is disabled, approval policy is never, MCP config is empty, user config and
project rules are ignored, host skills are skipped. Feature-disable flags target shell, code mode, browser,
image, apps, plugins, subagents, hooks and memories. The live audit showed these
flags do NOT establish a tool-less session on this version; the workload gate
is the enforcing control until a supported structural restriction is found.
Final text is read from its dedicated file; JSON events and stderr are never
returned as text or logged. Any tool/unknown event rejects the result. Timeouts
kill the complete CLI process group. No provider retries; budgets are bounded.

After structural qualification is established, before a workload a fresh synthetic marker outside cwd must remain unreadable
in a canary with zero tool events. Failed qualification latches unavailable for
the process lifetime; successful qualification is cached per executable identity,
model and login location. This canary supplements the flags; it does not replace
them. After any CLI upgrade, review restrictions and requalify before use.

## Verification without production

```sh
source .venv/bin/activate
python -m self_modify.canonical_runner
python scripts/probe_codex_provider.py
```

The second command runs hermetic pytest; the third performs exactly two direct
synthetic inferences (text and capability canary), no database or workload calls.
It prints fixed verdicts only. Never weaken restrictions to make a probe pass.

Audit result: 1,678 hermetic tests passed, 37 added, 2 skipped, 23.6 s.
Both direct probes failed with `tool_activity`; no real workload was sent.
Configuration reference: https://learn.chatgpt.com/docs/config-file/config-reference
