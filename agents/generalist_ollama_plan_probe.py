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
import httpx
from loguru import logger

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_generic_read import extract_openapi_read, validate_response
from agents.generalist_mission_planner import (
    MissionRequirements, OllamaProposalSource, ProposalEvidence, load_fixture_catalog,
    validate_planning_candidate,
)
from core.llm_client import OllamaLLMClient


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests" / "fixtures" / "generalist_discovery_catalog_v1.json"
MISSION = "Locate a public catalog record for a chosen novel; report its name and available details"
REQUIREMENTS = MissionRequirements(MISSION, "detail", ("title",))
_FAILURES = frozenset({
    "LOCAL_CONFIG_REJECTED", "MODEL_NOT_INSTALLED", "OLLAMA_UNREACHABLE",
    "INFERENCE_TIMEOUT", "MODEL_SERVICE_ERROR", "RESPONSE_MISSING_OR_INCOMPLETE",
    "MODEL_FORMAT_UNSUPPORTED", "GENERATION_BUDGET_EXHAUSTED",
    "INVALID_JSON", "DUPLICATE_JSON_KEY", "INVALID_PROPOSAL_SCHEMA", "UNKNOWN_CATALOG_OR_OPERATION",
    "MISSION_CAPABILITY_INCOMPATIBLE", "DOCUMENTATION_VALIDATION_FAILED",
    "FORBIDDEN_TOOL_ACTIVITY", "PLANNING_FAILURE_UNKNOWN",
})
_DOCUMENT_FAILURES = frozenset({"CATALOG_INVALID", "DOCUMENT_SOURCE_UNVERIFIED",
                                "DOCUMENT_INVALID", "EXTERNAL_RESPONSE_INVALID",
                                "UNREVIEWED_DESTINATION"})


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


async def _one_proposal(evidence: ProposalEvidence) -> None:
    """Run one bounded proposal and validate its selected documented operation."""
    try:
        config = _local_model_config()
    except (CapabilityRefusal, OSError, ValueError):
        raise CapabilityRefusal("LOCAL_CONFIG_REJECTED") from None
    catalog = load_fixture_catalog(CATALOG)
    # Fail before inference if a pinned operation or snapshot has drifted.
    catalog.operation_choices()
    # The existing client logs full payloads at DEBUG. This dedicated process
    # removes the logger sink before any request so raw prompt/response stays local.
    logger.remove()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    client = OllamaLLMClient(config, timeout=90)
    try:
        try:
            models = await asyncio.wait_for(client.list_models(), timeout=5)
        except (TimeoutError, httpx.ConnectError, httpx.NetworkError):
            raise CapabilityRefusal("OLLAMA_UNREACHABLE") from None
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            raise CapabilityRefusal("MODEL_SERVICE_ERROR") from None
        requested = config.ollama_primary_model
        available = {item.name for item in models}
        if requested not in available and (":" in requested or requested + ":latest" not in available):
            raise CapabilityRefusal("MODEL_NOT_INSTALLED")
        proposal = await OllamaProposalSource(client, config.ollama_primary_model, evidence).propose(
            MISSION, catalog)
        selected = validate_planning_candidate(catalog, proposal, REQUIREMENTS, MISSION,
                                               evidence=evidence)
        contract = extract_openapi_read(selected.document)
        validate_response(contract, selected.entry.isolated_sample)
    finally:
        await client.close()


def main() -> int:
    """Require explicit operator consent, then emit only fixed bounded facts."""
    if len(sys.argv) != 1 or not _foreground_consent():
        sys.stdout.write(json.dumps({"VERDICT": "LOCAL_CONSENT_REQUIRED",
                                     "PRODUCTION_QUALIFIED": False}) + "\n")
        return 2
    evidence = ProposalEvidence()
    try:
        asyncio.run(_one_proposal(evidence))
        failure, code = "VALID_PROPOSAL", 0
    except CapabilityRefusal as exc:
        failure, code = (exc.code if exc.code in _FAILURES else
                         "DOCUMENTATION_VALIDATION_FAILED" if exc.code in _DOCUMENT_FAILURES else
                         "PLANNING_FAILURE_UNKNOWN"), 2
    except (OSError, ValueError, TimeoutError, TypeError, KeyError):
        failure, code = "PLANNING_FAILURE_UNKNOWN", 2
    result = {"VERDICT": "MODEL_PROPOSAL_VALIDATED" if code == 0 else "MODEL_PROPOSAL_BLOCKED",
              "FAILURE_CLASS": failure,
              "INFERENCE_ATTEMPTED": evidence.inference_attempted,
              "RESPONSE_RECEIVED": evidence.response_received,
              "VALID_JSON": evidence.json_valid,
              "VALID_PROPOSAL_SCHEMA": evidence.proposal_validated,
              "CATALOG_OPERATION_VALIDATED": evidence.catalog_operation_validated,
              "MISSION_COMPATIBLE": evidence.mission_compatible,
              "AUTHORIZATION_GRANTED": False,
              "MODEL_PROPOSAL": evidence.proposal_validated,
              "INDEPENDENT_CONTRACT_VALIDATION": code == 0,
              "EXECUTED_CAPABILITY": False, "MISSION_COMPLETED": False,
              "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
