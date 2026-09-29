"""Read-only source/unit measurement; never changes extraction or gate verdicts.

Numbers use the EXISTING fidelity parser. Reference candidates are restricted to
Domain-declared corresponding fields actually read by the objective, never the
latest global snapshot. No guessed currency conversions or source identities.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import math
import re
from typing import Any

from core.domain import current_domain
from core.field_confusion import phrase_to_fields, SINGLE_NUMERIC_FIELD
from core.numeric_fidelity import _find_value_match, _MATCH_TOLERANCE, _explicit_scale


@dataclass(frozen=True)
class Citation:
    """A fidelity-parsed token with original offsets and explicit display units."""
    token: str
    value: float
    start: int
    end: int
    units: frozenset[str]
    scale: float


def citations(text: str) -> list[Citation]:
    """Iterate the existing fidelity parser, retaining comma-aware source spans."""
    # Preserve coordination commas as whitespace before calling the fidelity
    # parser. Numeric grouping commas retain that parser's existing removal.
    positions = []
    characters = []
    for i, char in enumerate(text):
        if char == "," and i and i + 1 < len(text) and text[i - 1].isdigit() and text[i + 1].isdigit():
            continue
        positions.append(i)
        characters.append(" " if char == "," else char)
    cleaned = "".join(characters)
    cursor = 0
    out = []
    while cursor < len(cleaned):
        match = _find_value_match(cleaned[cursor:])
        if match is None:
            break
        a, b = cursor + match.start(), cursor + match.end()
        start, end = positions[a], positions[b - 1] + 1
        token = match.group(0)
        cursor = b
        units = set()
        left = re.search(r"(?:\$|(?<!\w)(?:USD|BTC))\s*$", text[:start], re.I)
        right = re.match(r"\s*(%|percent\b|USD\b|BTC\b)", text[end:], re.I)
        for mark in (left.group(0).strip() if left else "", right.group(1) if right else ""):
            if mark:
                units.add({"$": "usd", "%": "percent"}.get(mark.lower(), mark.lower()))
        tail = re.split(r"[;,]", text[end:end + 16], maxsplit=1)[0]
        scale = _explicit_scale(token + tail, token)
        value = float(token)
        if math.isfinite(value):
            out.append(Citation(token, value, start, end, frozenset(units), scale))
    return out


_BOUNDARY = re.compile(
    r"\b(?:compared\s+(?:to|with)|vs\.?|versus|while|whereas|than|against|and|but|or)\b|[,;!?]|\.(?=\s|$)",
    re.I,
)


def _clause(text: str, citation: Citation, numbers: list[Citation]) -> tuple[int, int]:
    boundaries = [m for m in _BOUNDARY.finditer(text)
                  if not any(n.start <= m.start() < n.end for n in numbers)]
    start = max((m.end() for m in boundaries if m.end() <= citation.start), default=0)
    end = min((m.start() for m in boundaries if m.start() >= citation.end), default=len(text))
    return start, end


def _aliases(clause: str) -> set[str]:
    found = set()
    for source, names in getattr(current_domain(), "source_aliases", {}).items():
        if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", clause, re.I) for name in names):
            found.add(source)
    return found


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def matches_reference(citation: Citation, reference: float, unit: str | None) -> bool:
    """Fidelity tolerance OR reference rounded to the written display precision.

    Percent ↔ fraction is the ONLY unit conversion, explicitly authorized by the
    field unit. Magnitude suffixes reuse the fidelity parser's explicit scaling.
    Decimal half-even rounding makes the precise 0.00005/0.0001 tie a mismatch.
    """
    factor = 100.0 if unit == "fraction" and "percent" in citation.units else 1.0
    displayed = reference * factor / citation.scale
    if not math.isfinite(displayed):
        return False
    if displayed == 0:
        tolerance_match = abs(citation.value) < 1e-9
    else:
        tolerance_match = abs(citation.value - displayed) / abs(displayed) <= _MATCH_TOLERANCE
    if tolerance_match:
        return True
    try:
        written = Decimal(citation.token)
        with localcontext() as ctx:
            ctx.prec = 64
            return Decimal(str(displayed)).quantize(Decimal(1).scaleb(written.as_tuple().exponent)) == written
    except (InvalidOperation, ValueError):
        return False


def references_from_objective(evidence: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Keep every recorded objective reading; failures identify attempts, not values."""
    references: dict[str, list[dict[str, Any]]] = defaultdict(list)
    read_tools: set[str] = set()
    for entry in evidence or []:
        if not isinstance(entry, dict) or entry.get("type") != "cycle_payload":
            continue
        for item in entry.get("tool_results") or []:
            if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
                continue
            tool = item["tool"]
            read_tools.add(tool)
            if item.get("success") is False or item.get("error"):
                continue
            if isinstance(item.get("result"), dict):
                references[tool].append(item["result"])
    return dict(references), read_tools


def _evidence_source(citation: Citation, evidence: list[dict[str, Any]]) -> str | None:
    """Only evidence containing this value can attribute an ambiguous claim token."""
    identities = getattr(current_domain(), "tool_sources", {})
    matches = set()
    for item in evidence:
        tool = str(item.get("source") or "")
        for other in citations(str(item.get("detail") or "")):
            if Decimal(citation.token) * Decimal(str(citation.scale)) == Decimal(other.token) * Decimal(str(other.scale)):
                source = identities.get(tool)
                if source:
                    matches.add(source)
    return next(iter(matches)) if len(matches) == 1 else None


def _unit_wrong(citation: Citation, unit: str | None, reference: float) -> bool:
    if not unit or not citation.units:
        return False
    if (unit == "btc" and "usd" in citation.units) or (unit == "usd" and "btc" in citation.units):
        return True
    if unit == "fraction" and "percent" in citation.units:
        # A correctly converted percent citation always wins. Flag direct
        # fraction-as-percent only if the unconverted value actually matches.
        bare = Citation(citation.token, citation.value, citation.start, citation.end, frozenset(), citation.scale)
        return not matches_reference(citation, reference, unit) and matches_reference(bare, reference, unit)
    return False


def measure_thesis(
    thesis: dict[str, Any],
    payloads: dict[str, dict[str, Any] | list[dict[str, Any]]] | None,
    *, read_tools: set[str] | None = None,
    gate_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return per-citation diagnostics and a validated comparable cross-source flag.

    Counts refer to distinct (value, source, semantic context, display unit)
    citations; identical claim/evidence repetitions count once. Missing historical
    references are UNVERIFIED, never evidence that a source was not read.
    """
    domain = current_domain()
    identities = getattr(domain, "tool_sources", {})
    units = getattr(domain, "field_units", {})
    contexts = getattr(domain, "field_contexts", {})
    refs = {tool: value if isinstance(value, list) else [value] for tool, value in (payloads or {}).items()}
    seen_tools = read_tools if read_tools is not None else set(refs)
    read_sources = {identities[t] for t in seen_tools if t in identities}
    known_reading = bool(seen_tools)
    evidence = [e for e in thesis.get("evidence") or [] if isinstance(e, dict)]
    texts = [("claim", str(thesis.get("claim") or ""), None)]
    texts += [("evidence", str(e.get("detail") or ""), e) for e in evidence]
    records: list[dict[str, Any]] = []
    dedup = set()
    comparable: dict[tuple[str, str], set[str]] = defaultdict(set)
    for origin, text, evidence_item in texts:
        previous_context = ""
        numbers = citations(text)
        for number in numbers:
            clause_start, clause_end = _clause(text, number, numbers)
            clause = text[clause_start:clause_end]
            local_start = max((n.end for n in numbers if clause_start <= n.start and n.end <= number.start), default=clause_start)
            local_context = text[local_start:number.start]
            postfix_end = min((n.start for n in numbers if number.end <= n.start < clause_end), default=clause_end)
            postfix_context = text[number.end:postfix_end]
            aliases = _aliases(clause)
            source = next(iter(aliases)) if len(aliases) == 1 else None
            attribution = "clause" if source else "unattributed"
            if source is None:
                source = identities.get(str(evidence_item.get("source") or "")) if evidence_item else _evidence_source(number, evidence)
                if source:
                    attribution = "evidence"
            # Local semantic field first, then ellipsis from the preceding clause,
            # then the thesis subject. None of these fallbacks attributes a source.
            fields_by_tool = {}
            used_context = clause
            for context in (local_context, previous_context, str(thesis.get("subject") or ""), postfix_context):
                mapped = {t: phrase_to_fields(t, context) if t not in SINGLE_NUMERIC_FIELD or identities.get(t) == source else frozenset() for t in contexts}
                if any(mapped.values()):
                    fields_by_tool = mapped
                    used_context = context
                    break
            previous_context = used_context
            semantic = {contexts[t][f] for t, fields in fields_by_tool.items()
                        if source is None or identities.get(t) == source
                        for f in fields if f in contexts[t]}
            key = (number.value * number.scale, source, tuple(sorted(semantic)), tuple(sorted(number.units)))
            if key in dedup:
                continue
            dedup.add(key)
            record = {"value": number.value, "written": number.token, "source": source,
                      "attribution": attribution, "origin": origin, "contexts": sorted(semantic),
                      "source_misattribution": False, "unit_mismatch": False,
                      "reference": None, "own_references": [], "gate_rewrites": []}
            for event in gate_events or []:
                if event.get("action") != "rewrite" or event.get("subject") != thesis.get("subject"):
                    continue
                if any(_number(event.get(k)) is not None and abs(number.value - float(event[k])) <= max(abs(number.value) * _MATCH_TOLERANCE, 1e-12) for k in ("cited_value", "true_value")):
                    record["gate_rewrites"].append({k: event.get(k) for k in ("tool", "cited_value", "true_value", "reason")})
            records.append(record)
            if not source or not semantic:
                record["verdict"] = "UNATTRIBUTED" if not source else "UNVERIFIED"
                continue
            if re.search(r"\b(?:spread|difference|minus)\b", clause, re.I):
                record["verdict"] = "DERIVED_UNVERIFIED"
                continue
            candidates = []
            for tool, readings in refs.items():
                for field, context in contexts.get(tool, {}).items():
                    # Resolve the phrase separately for each source: specific
                    # 8h/mark/etc restricts that source; generic phrases allow ties.
                    if context not in semantic or field not in fields_by_tool.get(tool, frozenset()):
                        continue
                    for reading in readings:
                        if not isinstance(reading, dict):
                            continue
                        value = _number(reading.get(field))
                        if value is not None:
                            candidates.append({"tool": tool, "field": field, "source": identities.get(tool),
                                               "value": value, "unit": units.get(tool, {}).get(field), "context": context})
            own = [r for r in candidates if r["source"] == source]
            record["own_references"] = own
            own_matches = [r for r in own if matches_reference(number, r["value"], r["unit"])]
            clean_matches = [r for r in own_matches if not _unit_wrong(number, r["unit"], r["value"])]
            if clean_matches:
                record.update(verdict="CLEAN", reference=clean_matches[0])
                for ref in clean_matches:
                    if ref["unit"]:
                        comparable[(ref["context"], ref["unit"])].add(source)
                continue  # An own-source valid match ALWAYS wins.
            unit_errors = [r for r in own if _unit_wrong(number, r["unit"], r["value"])]
            if unit_errors:
                record.update(unit_mismatch=True, reference=unit_errors[0])
            # Don't treat a unit mistake as evidence of a different source.
            others = [r for r in candidates if r["source"] and r["source"] != source
                      and matches_reference(number, r["value"], r["unit"]) and not _unit_wrong(number, r["unit"], r["value"])]
            if known_reading and source not in read_sources:
                record.update(source_misattribution=True, reason="source_not_read", reference=others[0] if others else None)
            elif not own_matches and not unit_errors and own and others:
                record.update(source_misattribution=True, reason="corresponding_other_source_field", reference=others[0])
            record["verdict"] = ("SOURCE_MISATTRIBUTION" if record["source_misattribution"] else
                                 "UNIT_MISMATCH" if record["unit_mismatch"] else "UNVERIFIED")
    return {"cross_source": any(len(sources) >= 2 for sources in comparable.values()), "citations": records}
