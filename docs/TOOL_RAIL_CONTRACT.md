# Domain tool-rail contract

```
                 Tool Catalog
                /            \
        installed A       installed B
               \             /
                 Domain Rail
                     |
             Effective Tool Set
                     |
        registration / exposure / execution
```

**Installed ≠ Active.** `tools.discovery` enumerates installed data-feed implementations without deciding authority. `core.tool_rail` owns one static classification for globally available internal tools and statically installed research tools. Each Domain pack owns its independent `rail.tools` allowlist for installed research/data tools. The effective set is Global Core Tools ∪ Domain Rail. An omitted rail means no Domain data tools; it never means every discovered tool.

The crypto pack explicitly declares every previously callable research tool, including `get_crypto_history` (callable for bootstrap checks but not a chat/source tool). `web_search`, FRED observations/search and technical analysis are Domain research tools, not universal internal capabilities. Objective, memory, notification, file and code-executor primitives retain their historical global registration; only objective/memory primitives appear in the chat set. Tool flags still describe whether an *active* data-feed tool is a source or chat tool; flags alone confer no membership.

The Domain loader rejects duplicate, malformed, uninstalled and global-tool names in `rail.tools`. Acquisition metadata (`source_cache_config`, cache args, `metric_collections`, bootstrap tool defaults and legacy source-tool references) cannot name an inactive tool. Measurement metadata remains descriptive; it never grants membership. Historical measurement-only entries that cannot acquire data are not interpreted as rail declarations.

`api.server.build_tool_router` registers only effective tools and fails if an authorized implementation is missing. `ToolRouter` checks policy again on registration, schema lookup and every execution. Direct calls return `TOOL_NOT_ALLOWED_FOR_DOMAIN` for an installed inactive tool and `UNKNOWN_TOOL` for an absent one, with no filesystem or credential details. `/api/tools` lists active tools and effective source/chat labels. `/api/tools/catalog` reports installation and active status separately. The autonomous Brain, objective-generation context, wiki labels, cache/metric collectors and measurement diagnostics use the effective rail or validated Domain acquisition declarations.

## Auto-extension lifecycle

Proposal → gates → **human approval** → apply/install → installed tool → separate declarative Domain activation → active rail.

Apply still runs its tests, commits locally and performs its existing service-restart/rollback sequence when a human invokes it. Its post-apply probe now checks readiness plus the installed catalog endpoint, so an approved implementation does not have to be active in the current Domain. Apply does not edit `domain.yaml`, and this change adds no activation CLI. No tool becomes cross-domain merely because its Python module exists. A later activation change must be reviewed and tested for the selected Domain; Project selection itself does not alter a running process's Domain.

The synthetic two-Project proof in `tests/test_tool_rail_contract.py` installs both fixture tools in each fresh process, selects one per Domain, confirms only that tool enters the registered/API/chat/source sets, and checks that direct registration, schema lookup and execution of the other tool are denied. It builds explicit child environments and never touches production DB, LLMs or network services.
