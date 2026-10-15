"""Operator-consented one-call local Ollama proposal check; no execution grant."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from dotenv import dotenv_values
from loguru import logger

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_generic_read import extract_openapi_read
from agents.generalist_mission_planner import (
    OllamaProposalSource, load_fixture_catalog,
)
from core.llm_client import OllamaLLMClient


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests" / "fixtures" / "generalist_discovery_catalog_v1.json"
MISSION = ("Find public bibliographic information for a book and summarize "
           "its title and publication metadata")


def _foreground_consent() -> bool:
    """Require a controlling foreground terminal and typed local consent."""
    if (any(os.environ.get(key) for key in ("CI", "GITHUB_ACTIONS", "CODEBUILD_BUILD_ID"))
            or any(not os.isatty(fd) for fd in (0, 1, 2))):
        return False
    reader = None
    writer = None
    try:
        foreground_group = os.getpgrp()
        stream_devices = [os.fstat(fd).st_rdev for fd in (0, 1, 2)]
        if (len(set(stream_devices)) != 1
                or any(not stat.S_ISCHR(os.fstat(fd).st_mode)
                       or os.tcgetpgrp(fd) != foreground_group
                       for fd in (0, 1, 2))):
            return False
        # /dev/tty is an alias (device 5:0), not the /dev/pts/N device of the
        # standard streams. Separate handles avoid r+ seeking on a PTY.
        reader = open("/dev/tty", "r", encoding="utf-8", buffering=1)
        writer = open("/dev/tty", "w", encoding="utf-8", buffering=1)
        if (not os.isatty(reader.fileno()) or not os.isatty(writer.fileno())
                or os.tcgetpgrp(reader.fileno()) != foreground_group
                or os.tcgetpgrp(writer.fileno()) != foreground_group):
            return False
        writer.write("One synthetic local Ollama planning proposal; no external API call. Type PLAN ONCE: ")
        writer.flush()
        answer = reader.readline(40)
        return answer.removesuffix("\n").removesuffix("\r") == "PLAN ONCE"
    except (OSError, ValueError, UnicodeError):
        return False
    finally:
        try:
            if writer is not None:
                writer.close()
        finally:
            if reader is not None:
                reader.close()


def _local_model_config() -> SimpleNamespace:
    """Read only local Ollama settings; refuse remote/proxied model endpoints."""
    config_path = Path(os.environ.get("MORGOTH_LOCAL_CONFIG", str(ROOT / ".env")))
    expected = ROOT.parent / "morgoth" / ".env"
    if (config_path not in {ROOT / ".env", expected} or
            (config_path.exists() and (config_path.is_symlink() or
                                       config_path.stat().st_size > 16384))):
        raise CapabilityRefusal("LOCAL_MODEL_CONFIG_UNAVAILABLE")
    values = dotenv_values(config_path) if config_path.is_file() else {}
    base = os.environ.get("OLLAMA_BASE_URL") or values.get("OLLAMA_BASE_URL")
    model = os.environ.get("OLLAMA_PRIMARY_MODEL") or values.get("OLLAMA_PRIMARY_MODEL")
    if type(base) is not str or type(model) is not str:
        raise CapabilityRefusal("LOCAL_MODEL_CONFIG_UNAVAILABLE")
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or parsed.port is None
            or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", model) is None):
        raise CapabilityRefusal("LOCAL_MODEL_CONFIG_UNAVAILABLE")
    return SimpleNamespace(ollama_base_url=base, ollama_primary_model=model,
                           ollama_num_ctx=4096)


async def _one_proposal() -> dict[str, object]:
    """Run one bounded proposal and validate its selected documented operation."""
    config = _local_model_config()
    catalog = load_fixture_catalog(CATALOG)
    # The existing client logs full payloads at DEBUG. This dedicated process
    # removes the logger sink before any request so raw prompt/response stays local.
    logger.remove()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    client = OllamaLLMClient(config, timeout=20)
    try:
        proposal = await OllamaProposalSource(client, config.ollama_primary_model).propose(
            MISSION, catalog)
        entry = catalog.get(proposal.catalog_id)
        if catalog.select(MISSION).catalog_id != entry.catalog_id:
            raise CapabilityRefusal("PROPOSED_SOURCE_UNSUPPORTED")
        selected = catalog.load(entry)
        contract = extract_openapi_read(selected.document)
        if proposal.operation_id != contract.operation_id:
            raise CapabilityRefusal("PROPOSED_OPERATION_UNSUPPORTED")
        return {"VERDICT": "MODEL_PROPOSAL_VALIDATED",
                "MODEL_PROPOSAL": True, "INDEPENDENT_CONTRACT_VALIDATION": True,
                "EXECUTED_CAPABILITY": False, "MISSION_COMPLETED": False,
                "PRODUCTION_QUALIFIED": False}
    finally:
        await client.close()


def main() -> int:
    """Require explicit operator consent, then emit only fixed bounded facts."""
    if len(sys.argv) != 1 or not _foreground_consent():
        sys.stdout.write(json.dumps({"VERDICT": "LOCAL_CONSENT_REQUIRED",
                                     "PRODUCTION_QUALIFIED": False}) + "\n")
        return 2
    try:
        result = asyncio.run(_one_proposal())
        code = 0
    except (CapabilityRefusal, OSError, ValueError, TimeoutError):
        result = {"VERDICT": "MODEL_PROPOSAL_BLOCKED", "MODEL_PROPOSAL": False,
                  "INDEPENDENT_CONTRACT_VALIDATION": False,
                  "EXECUTED_CAPABILITY": False, "MISSION_COMPLETED": False,
                  "PRODUCTION_QUALIFIED": False}
        code = 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
