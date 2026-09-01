"""Provider abstraction for LLM calls.

Public surface:
  · resolve(task) → (provider_name, model_name)
  · providers.get_provider(name) → Provider instance
  · Task strings live as constants in `tasks`.

Ship goal (this slice): make provider choice a config decision, per task,
swappable without code changes. NO default changes — every task resolves
to exactly the provider it used before this abstraction landed, unless
the operator sets MORGOTH_LLM_<TASK>=provider:model.
"""

from core.llm import providers, registry, tasks
from core.llm.registry import resolve
from core.llm.providers import Provider, get_provider

__all__ = ["providers", "registry", "tasks", "Provider", "get_provider", "resolve"]
