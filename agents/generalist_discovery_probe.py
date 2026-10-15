"""One foreground-consented local-model discovery evaluation; no execution."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
from loguru import logger

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_discovery import (
    OfflineDiscovery, OllamaDocumentChoiceSource, OllamaIntentSource, SearchHit,
)
from agents.generalist_generic_read import extract_openapi_read, validate_response
from agents.generalist_mission_planner import MissionRequirements, load_fixture_catalog
from agents.generalist_ollama_plan_probe import _foreground_consent, _local_model_config
from core.llm_client import OllamaLLMClient


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "morgoth-generalist-autonomous-learning-v1"
CORPUS = ROOT / "tests" / "fixtures" / "generalist_v8_heldout_catalog.json"
MISSION = "Find a novel's public catalog title and other available details"
REQUIREMENTS = MissionRequirements(MISSION, "detail", ("title",))


def _checkout_verified() -> bool:
    """Refuse uncommitted, untracked or non-origin operator execution."""
    try:
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        return (git("branch", "--show-current") == BRANCH
                and git("remote", "get-url", "origin") == "git@github.com:coriom/morgoth.git"
                and git("rev-parse", "HEAD") == git("rev-parse", f"origin/{BRANCH}")
                and not git("status", "--porcelain=v1", "--untracked-files=all"))
    except (OSError, subprocess.SubprocessError):
        return False


async def _evaluate() -> dict[str, object]:
    """Run at most two local planning calls with offline reviewed evidence."""
    report: dict[str, object] = {
        "VERDICT": "MODEL_DISCOVERY_BLOCKED", "INTENT_PROPOSED": False,
        "DOCUMENT_DISCOVERED": False, "SOURCE_GROUNDED": False,
        "DOCUMENT_CHOICE_PROPOSED": False,
        "CONTRACT_VALIDATED": False, "ISOLATED_TEST_PASSED": False,
        "APPROVAL_PENDING": False, "LEXICAL_TOP_MATCHES_SELECTED": False,
        "AUTHORIZATION_GRANTED": False, "EXECUTED_CAPABILITY": False,
        "MODEL_CALL_COUNT": 0, "PRODUCTION_QUALIFIED": False,
    }
    try:
        config = _local_model_config()
        catalog = load_fixture_catalog(CORPUS)
    except (CapabilityRefusal, OSError, ValueError):
        report["FAILURE_CLASS"] = "PREFLIGHT_REJECTED"
        return report
    logger.remove()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    client = OllamaLLMClient(config, timeout=90)
    try:
        try:
            models = await asyncio.wait_for(client.list_models(), timeout=5)
            names = {item.name for item in models}
            requested = config.ollama_primary_model
            if requested not in names and requested + ":latest" not in names:
                raise CapabilityRefusal("MODEL_NOT_INSTALLED")
        except (TimeoutError, httpx.HTTPError, OSError, ValueError):
            report["FAILURE_CLASS"] = "OLLAMA_PREFLIGHT_UNAVAILABLE"
            return report
        except CapabilityRefusal as exc:
            report["FAILURE_CLASS"] = exc.code
            return report
        if not _foreground_consent():
            report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
            return report
        report["MODEL_CALL_COUNT"] = 1
        try:
            intent = await OllamaIntentSource(client, requested).interpret(MISSION, ())
            report["INTENT_PROPOSED"] = True
            if intent.abstain:
                raise CapabilityRefusal("INTENT_ABSTAINED")
            if (intent.shape != REQUIREMENTS.shape or
                not set(REQUIREMENTS.required_result_fields).issubset(intent.required_fields)):
                raise CapabilityRefusal("INTENT_ORACLE_MISMATCH")
            discovery = OfflineDiscovery(catalog)
            hits = discovery.search(intent.search_terms)
            if not hits:
                raise CapabilityRefusal("DOCUMENTATION_NOT_FOUND")
            report["DOCUMENT_DISCOVERED"] = True
            report["MODEL_CALL_COUNT"] = 2
            choice = await OllamaDocumentChoiceSource(client, requested).choose(
                MISSION, discovery.metadata(hits))
            report["DOCUMENT_CHOICE_PROPOSED"] = True
            if choice.abstain:
                raise CapabilityRefusal("DOCUMENT_CHOICE_ABSTAINED")
            if choice.catalog_id not in {hit.catalog_id for hit in hits}:
                raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID")
            documents = discovery.retrieve((SearchHit(choice.catalog_id, 1),))
            compatible = []
            for selected in documents:
                contract = extract_openapi_read(selected.document)
                try:
                    REQUIREMENTS.check(MISSION, contract)
                except CapabilityRefusal:
                    continue
                compatible.append((selected, contract))
            if len(compatible) != 1:
                raise CapabilityRefusal("DOCUMENTATION_AMBIGUOUS" if compatible else
                                        "DOCUMENTATION_INCOMPATIBLE")
            selected, contract = compatible[0]
            report["SOURCE_GROUNDED"] = True
            report["LEXICAL_TOP_MATCHES_SELECTED"] = hits[0].catalog_id == selected.entry.catalog_id
            report["CONTRACT_VALIDATED"] = True
            validate_response(contract, selected.entry.isolated_sample)
            report["ISOLATED_TEST_PASSED"] = True
            report["APPROVAL_PENDING"] = True
            report["VERDICT"] = "MODEL_DISCOVERY_VALIDATED_PENDING_APPROVAL"
            report["FAILURE_CLASS"] = "NONE"
        except CapabilityRefusal as exc:
            allowed = {"MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_RESPONSE_INCOMPLETE",
                       "INTENT_SCHEMA_INVALID", "FORBIDDEN_TOOL_ACTIVITY", "INTENT_ABSTAINED",
                       "INTENT_ORACLE_MISMATCH", "DOCUMENTATION_NOT_FOUND",
                       "DOCUMENTATION_AMBIGUOUS", "DOCUMENTATION_INCOMPATIBLE",
                       "DOCUMENT_CHOICE_ABSTAINED", "DOCUMENT_CHOICE_INVALID",
                       "DOCUMENT_INVALID", "DOCUMENT_SOURCE_UNVERIFIED",
                       "EXTERNAL_RESPONSE_INVALID", "CATALOG_INVALID"}
            report["FAILURE_CLASS"] = exc.code if exc.code in allowed else "EVALUATION_BLOCKED"
        except (OSError, ValueError, TypeError, KeyError):
            report["FAILURE_CLASS"] = "EVALUATION_BLOCKED"
        return report
    finally:
        await client.close()


def main() -> int:
    """Print bounded statuses only; never a prompt, response or document."""
    if len(sys.argv) != 1 or not _checkout_verified():
        print(json.dumps({"VERDICT": "CHECKOUT_REJECTED", "PRODUCTION_QUALIFIED": False}))
        return 2
    try:
        result = asyncio.run(_evaluate())
    except (OSError, ValueError, TypeError, httpx.HTTPError):
        result = {"VERDICT": "EVALUATION_BLOCKED", "PRODUCTION_QUALIFIED": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["VERDICT"] == "MODEL_DISCOVERY_VALIDATED_PENDING_APPROVAL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
