"""Parsed path grammar for digest_fields — never eval, always parse.

Unlocks nested sources like DefiLlama /protocols/stablecoins whose body
is a list of pegged assets. Backwards-compatible: a plain string entry
in ``digest_fields`` is still a top-level scalar key. The new shape is
``{"name": "<snake>", "path": "<expression>"}``.

Grammar (whitespace-insensitive between tokens):

    expr        := aggregate | path
    aggregate   := ('sum' | 'count' | 'min' | 'max') '(' path ')'
    path        := segment (('.' segment) | selector)*
    segment     := WORD
    selector    := '[' ( '*' | INT | WORD '=' VALUE ) ']'
    WORD        := [A-Za-z_][A-Za-z0-9_]*
    INT         := digits
    VALUE       := literal text until ']' (unquoted, single-token)

Semantics:

- Dotted keys navigate dicts.
- ``[N]`` selects the Nth element of a list (0-based).
- ``[key=value]`` picks the FIRST list element whose ``element[key]``
  string-equals ``value`` (both cast to str for comparison).
- ``[*]`` iterates a list; ONLY legal INSIDE an aggregate wrapper.
  Each element is resolved against the tail path.
- Elements that fail to yield the tail (missing key, wrong type) are
  SKIPPED — not counted, not summed, not errored. If every element
  under ``[*]`` misses, the aggregate returns 0 for ``count`` /
  ``sum`` and reports a resolution error for ``min`` / ``max``.
- Each expression MUST resolve to ONE SCALAR (int/float/str/bool).
  A path that lands on a dict/list is an error.

Injection: the parser only recognises the tokens above; VALUE inside
``[k=v]`` is stripped of ``]`` before matching and never fed to eval.
Field names are constrained by the caller's identifier regex.

Cap: MAX_DIGEST_FIELDS = 12 per tool — 2× the previous 3-6 top-level
limit, sized so a nested source with sum + count + a few picks fits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


MAX_DIGEST_FIELDS: int = 12

_TOKEN_RE = re.compile(
    r"\s*(?P<agg>sum|count|min|max)\s*\("
    r"|\s*\)"
    r"|\s*\.\s*"
    r"|\s*\[\s*"
    r"|\s*\]\s*"
    r"|\s*=\s*"
    r"|\s*\*\s*"
    r"|\s*(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\s*(?P<num>-?\d+)"
    r"|\s*(?P<val>[^\]\s=][^\]]*)"
)


class DigestPathError(ValueError):
    """Raised by parse() and resolve() on any grammar or resolution
    error. .segment names the failing path segment when known."""

    def __init__(self, message: str, segment: str | None = None) -> None:
        super().__init__(message)
        self.segment = segment


@dataclass
class Selector:
    kind: str  # "index" | "eq" | "star"
    index: int | None = None
    key: str | None = None
    value: str | None = None


@dataclass
class ParsedPath:
    """A parsed path or aggregate. Rendered form recovers the source
    verbatim (minus whitespace); use .render() for gate messages."""
    aggregate: str | None
    segments: list[Any]  # str (key) | Selector

    def has_star(self) -> bool:
        return any(isinstance(s, Selector) and s.kind == "star" for s in self.segments)

    def render(self) -> str:
        out = []
        for s in self.segments:
            if isinstance(s, str):
                if out and out[-1] not in ("[",):
                    out.append(".")
                out.append(s)
            elif s.kind == "star":
                out.append("[*]")
            elif s.kind == "index":
                out.append(f"[{s.index}]")
            elif s.kind == "eq":
                out.append(f"[{s.key}={s.value}]")
        inner = "".join(out)
        if inner.startswith("."):
            inner = inner[1:]
        if self.aggregate:
            return f"{self.aggregate}({inner})"
        return inner


def parse(expr: str) -> ParsedPath:
    """Parse an expression. Raises DigestPathError on any grammar error.
    A bare word (e.g. ``"symbol"``) parses to a single-segment path."""
    if not isinstance(expr, str) or not expr.strip():
        raise DigestPathError("empty path expression")
    text = expr.strip()
    # Aggregate wrapper?
    m = re.fullmatch(
        r"(sum|count|min|max)\s*\(\s*(.+?)\s*\)", text, re.DOTALL,
    )
    if m:
        aggregate = m.group(1)
        inner = m.group(2)
        p = _parse_bare_path(inner)
        if not p.has_star() and aggregate != "count":
            raise DigestPathError(
                f"{aggregate}() requires a [*] iterator inside", segment=aggregate,
            )
        p.aggregate = aggregate
        return p
    return _parse_bare_path(text)


def _parse_bare_path(text: str) -> ParsedPath:
    segments: list[Any] = []
    i = 0
    n = len(text)
    expect_key = True
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if expect_key and c.isalpha() or (expect_key and c == "_"):
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i:])
            if not m:
                raise DigestPathError(
                    f"invalid identifier at position {i}", segment=text[i:i+10],
                )
            segments.append(m.group(0))
            i += len(m.group(0))
            expect_key = False
            continue
        if c == ".":
            if expect_key:
                raise DigestPathError(
                    "unexpected '.'", segment=text[max(0, i-5):i+5],
                )
            i += 1
            expect_key = True
            continue
        if c == "[":
            # find matching ]
            j = text.find("]", i + 1)
            if j < 0:
                raise DigestPathError("unclosed '['", segment=text[i:])
            inner = text[i + 1:j].strip()
            segments.append(_parse_selector(inner, at=text[i:j+1]))
            i = j + 1
            expect_key = False
            continue
        raise DigestPathError(
            f"unexpected char {c!r} at position {i}", segment=text[max(0,i-3):i+3],
        )
    if not segments:
        raise DigestPathError("empty path")
    if not isinstance(segments[0], str):
        raise DigestPathError(
            "path must start with a key, not a selector", segment=str(segments[0]),
        )
    return ParsedPath(aggregate=None, segments=segments)


def _parse_selector(inner: str, at: str) -> Selector:
    if inner == "*":
        return Selector(kind="star")
    if re.fullmatch(r"-?\d+", inner):
        return Selector(kind="index", index=int(inner))
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)", inner)
    if m:
        return Selector(kind="eq", key=m.group(1), value=m.group(2).strip())
    raise DigestPathError(
        f"malformed selector {at!r}", segment=at,
    )


def resolve(path: ParsedPath, body: Any) -> Any:
    """Resolve a parsed path against a body. Returns the scalar, or
    raises DigestPathError naming the failing segment."""
    if path.aggregate:
        return _resolve_aggregate(path, body)
    return _resolve_scalar(path.segments, body)


def _resolve_scalar(
    segments: list[Any], body: Any, *, require_scalar: bool = True,
) -> Any:
    """Resolve a sequence of segments against body. When
    require_scalar=False the landing may be a list or dict — used by
    aggregate prefixes where the target IS a list to iterate over."""
    cur: Any = body
    for i, seg in enumerate(segments):
        if isinstance(seg, str):
            if not isinstance(cur, dict):
                raise DigestPathError(
                    f"segment {seg!r}: expected dict, got {type(cur).__name__}",
                    segment=seg,
                )
            if seg not in cur:
                raise DigestPathError(
                    f"segment {seg!r}: key missing from dict "
                    f"(keys: {sorted(cur.keys())[:6]})",
                    segment=seg,
                )
            cur = cur[seg]
            continue
        assert isinstance(seg, Selector)
        if not isinstance(cur, list):
            raise DigestPathError(
                f"selector {seg.kind}: expected list, got {type(cur).__name__}",
                segment=str(seg),
            )
        if seg.kind == "index":
            if seg.index is None or not (-len(cur) <= seg.index < len(cur)):
                raise DigestPathError(
                    f"index {seg.index} out of range (list has {len(cur)})",
                    segment=str(seg.index),
                )
            cur = cur[seg.index]
        elif seg.kind == "eq":
            found = None
            for el in cur:
                if isinstance(el, dict) and str(el.get(seg.key)) == str(seg.value):
                    found = el
                    break
            if found is None:
                raise DigestPathError(
                    f"[{seg.key}={seg.value}]: no list element matched",
                    segment=f"{seg.key}={seg.value}",
                )
            cur = found
        else:
            raise DigestPathError(
                "[*] iterator only legal inside sum()/count()/min()/max()",
                segment="[*]",
            )
    if require_scalar and isinstance(cur, (list, dict)):
        raise DigestPathError(
            f"path resolves to a {type(cur).__name__}, not a scalar",
            segment=(segments[-1] if isinstance(segments[-1], str) else "tail"),
        )
    return cur


def _resolve_aggregate(path: ParsedPath, body: Any) -> Any:
    # Split segments at the [*] into (prefix, tail).
    prefix: list[Any] = []
    tail: list[Any] = []
    seen_star = False
    for seg in path.segments:
        if isinstance(seg, Selector) and seg.kind == "star":
            seen_star = True
            continue
        (tail if seen_star else prefix).append(seg)
    if path.aggregate == "count" and not seen_star:
        # count(path) with no [*] → 1 if path resolves, 0 if it misses
        try:
            _resolve_scalar(prefix, body)
            return 1
        except DigestPathError:
            return 0
    try:
        listy = (
            _resolve_scalar(prefix, body, require_scalar=False)
            if prefix else body
        )
    except DigestPathError as exc:
        raise DigestPathError(f"aggregate prefix: {exc}", segment=exc.segment) from None
    if not isinstance(listy, list):
        raise DigestPathError(
            f"[*] target is {type(listy).__name__}, not list",
            segment="[*]",
        )
    values: list[float | int] = []
    for el in listy:
        try:
            v = _resolve_scalar(tail, el) if tail else el
        except DigestPathError:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            values.append(v)
    if path.aggregate == "count":
        return len(values) if tail else len(listy)
    if not values:
        if path.aggregate == "sum":
            return 0
        raise DigestPathError(
            f"{path.aggregate}(): no numeric elements after [*]",
            segment="[*]",
        )
    if path.aggregate == "sum":
        return sum(values)
    if path.aggregate == "min":
        return min(values)
    if path.aggregate == "max":
        return max(values)
    raise DigestPathError(f"unknown aggregate {path.aggregate!r}")


def parse_digest_entry(entry: Any) -> tuple[str, ParsedPath]:
    """Return (name, ParsedPath) from a digest_fields entry.

    Backwards-compatible: a plain string ``"symbol"`` is treated as a
    top-level scalar key with name = symbol. A dict entry must have
    exactly the keys {name, path} with snake-case name and a parseable
    path expression.
    """
    if isinstance(entry, str):
        return entry, parse(entry)
    if not isinstance(entry, dict):
        raise DigestPathError(
            f"digest entry must be str or {{name, path}} dict; got {type(entry).__name__}"
        )
    extra = set(entry.keys()) - {"name", "path"}
    if extra:
        raise DigestPathError(
            f"digest entry has unexpected keys {sorted(extra)}"
        )
    name = entry.get("name")
    path = entry.get("path")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,49}", name):
        raise DigestPathError("digest entry name must be snake_case identifier")
    if not isinstance(path, str):
        raise DigestPathError("digest entry path must be a string")
    return name, parse(path)


def resolve_digest_fields(
    digest_fields: list[Any], body: Any,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Resolve every entry in ``digest_fields``. Returns
    ``(values, errors)`` where values maps name → scalar for the
    successful entries and errors is [(name, message)] for the failures.
    The shape gate wraps this: any non-empty ``errors`` → rejected_shape.
    """
    values: dict[str, Any] = {}
    errors: list[tuple[str, str]] = []
    if not isinstance(digest_fields, list):
        return values, [("<digest_fields>", "must be a list")]
    if len(digest_fields) > MAX_DIGEST_FIELDS:
        return values, [(
            "<digest_fields>",
            f"cap exceeded: {len(digest_fields)} > MAX_DIGEST_FIELDS={MAX_DIGEST_FIELDS}",
        )]
    for entry in digest_fields:
        try:
            name, path = parse_digest_entry(entry)
        except DigestPathError as exc:
            errors.append((str(entry)[:60], f"parse: {exc}"))
            continue
        try:
            v = resolve(path, body)
        except DigestPathError as exc:
            errors.append((name, f"segment={exc.segment!r}: {exc}"))
            continue
        if isinstance(v, (list, dict)):
            errors.append((name, "resolves to non-scalar"))
            continue
        values[name] = v
    return values, errors
