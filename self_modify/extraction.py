"""Shared extraction contract for the shape gate + liveness probe.

The tool template (``TOOL_TEMPLATE`` in ``reflect.py``) reads digest
fields from ONE of three sites, in this order:

  1. Top-level dict — ``body[field]``.
  2. Nested ``data`` list — ``body["data"][0][field]``.
  3. Top-level list of dicts — ``body[0][field]``.

Both the shape gate (``_shape_check`` in reflect.py) and the
liveness probe (``self_modify.liveness``) must read the SAME site as
the template — otherwise the two gates and the eventual runtime
would disagree, and the probe would blindly PASS list-shaped bodies
whose rolling fields are actually frozen (the 1182ee96 hole).

This module hosts the single source of truth for that contract.
"""
from __future__ import annotations

from typing import Any


def template_extraction_site(
    body: Any, digest_fields: list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Return the dict the template will read fields from, plus a
    human-readable site label.

    Returns ``(None, reason)`` if the body's overall shape is
    unusable — neither a dict with matching top-level keys nor a
    ``{"data": [...]}`` fallback nor a top-level list-of-dicts.

    ``digest_fields`` is optional — it's used only to disambiguate
    a top-level dict from a wrapper (``if any(f in body ...)``); when
    omitted, a top-level dict is returned as-is.
    """
    digest_fields = digest_fields or []
    if isinstance(body, dict):
        if not digest_fields or any(f in body for f in digest_fields):
            return body, "top-level dict"
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0], "data[0] (nested)"
        # No digest key at top-level and no data-wrapper — the body is
        # a dict but the template will find nothing.
        return None, (
            "response is a dict but no digest field matches and there "
            "is no `data`: [<dict>] fallback"
        )
    if isinstance(body, list):
        if not body:
            return None, "response is an empty list"
        if not isinstance(body[0], dict):
            return None, (
                f"response is a list but its first element is a "
                f"{type(body[0]).__name__}, not a dict"
            )
        return body[0], "list[0] (top-level list)"
    return None, f"response is neither dict nor list (got {type(body).__name__})"
