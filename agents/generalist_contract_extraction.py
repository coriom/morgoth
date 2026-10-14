"""Data-only extraction of a tiny documented GET-operation subset.

This parser discovers shapes, not executable hosts or approval. A separately
reviewed driver and broker must bind any extracted operation to authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Literal

from agents.generalist_capability import CapabilityRefusal, _unique_pairs


@dataclass(frozen=True)
class ExtractedReadOperation:
    """Bounded declarative contract with no destination or credential field."""

    documentation_digest: str
    operation_id: str
    shape: Literal["collection", "detail"]
    path_template: str
    path_parameters: tuple[str, ...]
    query_parameters: tuple[str, ...]
    result_fields: tuple[str, ...]


def extract_read_operation(document: str) -> ExtractedReadOperation:
    """Parse one local machine-readable operation, never instructions/code."""
    if len(document.encode("utf-8")) > 8192:
        raise CapabilityRefusal("DOCUMENT_INVALID")
    try:
        raw = json.loads(document, object_pairs_hook=_unique_pairs)
        if type(raw) is not dict or set(raw) != {"morgoth_read_subset", "operation"}:
            raise ValueError("root shape")
        if raw["morgoth_read_subset"] != 1 or type(raw["operation"]) is not dict:
            raise ValueError("subset")
        op = raw["operation"]
        if set(op) != {"operation_id", "method", "shape", "path_template",
                       "path_parameters", "query_parameters", "result_fields"}:
            raise ValueError("operation shape")
        if op["method"] != "GET" or op["shape"] not in {"collection", "detail"}:
            raise ValueError("method or shape")
        identifier = r"[a-z][a-z0-9_]{0,31}"
        if type(op["operation_id"]) is not str or re.fullmatch(identifier, op["operation_id"]) is None:
            raise ValueError("operation id")
        path = op["path_template"]
        if (type(path) is not str or len(path) > 120 or not path.startswith("/")
                or "//" in path or ".." in path or "%" in path
                or any(char in path for char in "?#\\")
                or re.fullmatch(r"/[A-Za-z0-9_/{}/.-]+", path) is None):
            raise ValueError("path")
        groups = re.findall(r"\{([^{}]+)\}", path)
        if (path.count("{") != len(groups) or path.count("}") != len(groups)
                or any(re.fullmatch(identifier, name) is None for name in groups)):
            raise ValueError("path placeholder")
        names: list[tuple[str, ...]] = []
        for key, limit in (("path_parameters", 4), ("query_parameters", 4), ("result_fields", 12)):
            value = op[key]
            if (type(value) is not list or not 0 <= len(value) <= limit
                    or any(type(item) is not str or re.fullmatch(identifier, item) is None
                           for item in value) or len(set(value)) != len(value)):
                raise ValueError("field list")
            names.append(tuple(value))
        if (tuple(groups) != names[0] or not names[2]
                or (op["shape"] == "detail" and not groups)
                or (op["shape"] == "collection" and not names[1])):
            raise ValueError("shape mismatch")
        return ExtractedReadOperation(sha256(document.encode()).hexdigest(),
                                      op["operation_id"], op["shape"], path,
                                      names[0], names[1], names[2])
    except (ValueError, TypeError, KeyError):
        raise CapabilityRefusal("DOCUMENT_INVALID") from None
