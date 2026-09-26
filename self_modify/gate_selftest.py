"""Gate self-test — POSITIVE + NEGATIVE synthetic proposals through
gate_zone + gate_tests under full confinement.

Motivation (2026-09-27): unit tests of the digest-path resolver and
the shape-check helper proved those pieces work in isolation. They
did NOT prove that the FULL pipeline (zone classification → tree
copy → template render → bwrap+netns+cgroup pytest under
`-m "not integration"`) still lets a GOOD proposal through. Without
this end-to-end control, the fail-closed sandbox can silently
degrade into reject-everything without anyone noticing.

Two synthetic proposals, ephemeral (never touch self_modify_proposals):

  POSITIVE — a hand-written DefiLlama stablecoins spec with path
             digests, rendered via TOOL_TEMPLATE. gate_tests must
             PASS.
  NEGATIVE — the same file with a deliberate SyntaxError injected
             at module top so pytest collection fails. gate_tests
             must FAIL with tests_failed.

`morgoth reflect` calls run_selftest() at start (preflight): if the
positive control fails, reflect REFUSES to submit any proposal —
the gate is broken and needs the operator, not new work.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path("/home/corio/Morgoth/morgoth")


POSITIVE_SPEC: dict[str, Any] = {
    "tool_name": "get_selftest_positive_defillama",
    "api_base_url": "https://stablecoins.llama.fi",
    "endpoint_path": "/stablecoins",
    "digest_fields": [
        {"name": "total_supply",
         "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
        {"name": "usdt_supply",
         "path": "peggedAssets[symbol=USDT].circulating.peggedUSD"},
        {"name": "asset_count", "path": "count(peggedAssets[*])"},
    ],
    "description": (
        "Self-test positive control — DefiLlama stablecoins aggregate."
    ),
    "rationale": (
        "gate-selftest positive proposal; total_supply digest field "
        "resolves via path grammar."
    ),
}


@dataclass
class SelftestVerdict:
    label: str
    expected: str          # "pass" | "fail"
    actual_status: str     # gate_tests terminal status
    actual_reason: str
    ok: bool


def _render_content(spec: dict[str, Any], *, break_syntax: bool = False) -> str:
    """Render a valid tool file from the template, or (for the negative
    control) inject a syntax error at line 1 so pytest collection fails."""
    from self_modify.reflect import TOOL_TEMPLATE, _snake_to_class_name, _normalize_endpoint
    tool_name = spec["tool_name"]
    class_name = _snake_to_class_name(tool_name)
    from urllib.parse import urlparse
    source_label = urlparse(spec["api_base_url"]).hostname or ""
    endpoint_declaration = _normalize_endpoint(
        spec["api_base_url"], spec["endpoint_path"],
    )
    content = TOOL_TEMPLATE.format(
        tool_name=tool_name,
        class_name=class_name,
        tool_name_repr=repr(tool_name),
        base_url_repr=repr(spec["api_base_url"]),
        endpoint_path_repr=repr(spec["endpoint_path"]),
        digest_fields_repr=repr(list(spec["digest_fields"])),
        description_repr=repr(spec["description"]),
        source_label_repr=repr(source_label),
        endpoint_declaration_repr=repr(endpoint_declaration),
        requires_key_env_repr=repr(None),
        key_in_repr=repr(None),
        key_param_repr=repr(None),
    )
    if break_syntax:
        # Inject a bare `1 = 2` at the top so python collection fails.
        content = "1 = 2  # NEGATIVE CONTROL: syntax error injected\n" + content
    return content


class _EphemeralStore:
    """Fake ProposalStore that swallows all writes but keeps the
    final status + reason for the caller. Never touches the DB."""
    def __init__(self, pid: str) -> None:
        self.pid = pid
        self.status: str = ""
        self.reason: str = ""

    async def update_status(self, pid: str, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason


async def _run_one(label: str, spec: dict[str, Any], *, break_syntax: bool,
                    expected: str) -> SelftestVerdict:
    from self_modify import gates, proposals as P
    content = _render_content(spec, break_syntax=break_syntax)
    proposal = {
        "proposal_id": f"selftest-{label}",
        "target_path": f"tools/data_feeds/{spec['tool_name']}.py",
        "change_type": "new_file",
        "content": content,
    }
    store = _EphemeralStore(proposal["proposal_id"])
    zone = await gates.gate_zone(store, proposal)
    if zone != "green":
        actual = store.status or "zone_rejected"
    else:
        actual = await gates.gate_tests(store, proposal, repo_root=REPO_ROOT)
    ok_pass = (actual == P.STATUS_PENDING_APPROVAL)
    ok_fail = (actual in {
        P.STATUS_TESTS_FAILED, P.STATUS_ZONE_REJECTED,
        P.STATUS_REJECTED_SANDBOX_UNAVAILABLE,
    })
    ok = (expected == "pass" and ok_pass) or (expected == "fail" and ok_fail)
    return SelftestVerdict(
        label=label, expected=expected, actual_status=actual,
        actual_reason=store.reason[:400], ok=ok,
    )


async def run_selftest() -> tuple[SelftestVerdict, SelftestVerdict]:
    pos = await _run_one("positive", POSITIVE_SPEC,
                          break_syntax=False, expected="pass")
    neg = await _run_one("negative", POSITIVE_SPEC,
                          break_syntax=True, expected="fail")
    return pos, neg


def _print(v: SelftestVerdict) -> None:
    tag = "OK" if v.ok else "FAIL"
    print(f"  [{tag}] {v.label:<9} expected={v.expected}  "
          f"actual={v.actual_status}")
    if v.actual_reason:
        print(f"           reason: {v.actual_reason[:200]}")


def main() -> int:
    pos, neg = asyncio.run(run_selftest())
    print("═══ GATE SELF-TEST ═══")
    _print(pos)
    _print(neg)
    both_ok = pos.ok and neg.ok
    print(f"\nresult: {'PASS' if both_ok else 'FAIL'}")
    return 0 if both_ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
