"""Bounded diagnostic of a reviewed synthetic browser model request.

This is an offline analysis helper. It never calls a model, opens a browser,
or exports raw page text, objectives, typed values, prompts, or responses.
"""

from __future__ import annotations

from pathlib import Path

from agents.generalist_browser_lab import (
    BrowserRefusal, INTERACTIVE_SECONDS, MAX_ACTIONS, MAX_ELEMENTS,
    SyntheticTracePolicy,
)


def summarize_request(payload: object, schema: object,
                      checkout: Path, fixture: str) -> dict[str, object]:
    """Reduce a synthetic action request to fixture-scoped codes and fixed facts."""
    policy = SyntheticTracePolicy.from_checkout(checkout, fixture)
    try:
        if type(payload) is not dict or type(schema) is not dict:
            raise ValueError
        task, planning, current, observation = (
            payload["task"], payload["planning"], payload["current_evidence"],
            payload["observation"])
        if any(type(item) is not dict for item in (task, planning, current, observation)):
            raise ValueError
        objective, target = task["objective"], task["trusted_target"]
        revision, actions = observation["revision"], observation["actions"]
        remaining, seconds = planning["remaining_action_calls"], planning["remaining_seconds"]
        history = planning["verified_history"]
        if (type(objective) is not str or not 1 <= len(objective) <= 160 or
                type(target) is not str or not 1 <= len(target) <= 40 or
                type(revision) is not int or not 1 <= revision <= MAX_ACTIONS + 2 or
                type(remaining) is not int or not 0 <= remaining <= 6 or
                type(seconds) is not int or not 0 <= seconds <= INTERACTIVE_SECONDS or
                type(actions) is not list or not 1 <= len(actions) <= MAX_ELEMENTS + 1 or
                type(history) is not list or len(history) > 5 or
                type(current["target_in_visible_input"]) is not bool or
                type(planning["task_acceptance_passed"]) is not bool):
            raise ValueError
        candidates: list[dict[str, object]] = []
        ids: list[str] = []
        for action in actions:
            if type(action) is not dict:
                raise ValueError
            choice_id, kind, name = action["id"], action["kind"], action["name"]
            if (type(choice_id) is not str or type(kind) is not str or
                    type(name) is not str or kind not in {"click", "fill", "stop"}):
                raise ValueError
            if kind == "stop":
                if choice_id != "stop":
                    raise ValueError
                code = "STOP"
            else:
                code = policy.controls.get(name)
                if code is None:
                    raise ValueError
            ids.append(choice_id)
            candidate: dict[str, object] = {"CONTROL": code, "KIND": kind.upper()}
            if kind == "fill":
                value, authorized = action["current_value"], action["input_value"]
                if type(value) is not str or type(authorized) is not str:
                    raise ValueError
                candidate["CURRENT_EQUALS_AUTHORIZED"] = value == authorized
            candidates.append(candidate)
        effects: list[dict[str, str]] = []
        for item in history:
            if type(item) is not dict or item.get("effect") not in {
                    "UI_CHANGE_OBSERVED", "NO_OBSERVABLE_CHANGE"}:
                raise ValueError
            code = policy.controls.get(item.get("label"))
            if code is None or item.get("action") not in {"click", "fill"}:
                raise ValueError
            effects.append({"CONTROL": code, "KIND": item["action"].upper(),
                            "EFFECT": item["effect"]})
        schema_ids = schema["properties"]["choice_id"]["enum"]
        schema_revisions = schema["properties"]["revision"]["enum"]
        if type(schema_ids) is not list or type(schema_revisions) is not list:
            raise ValueError
        return {"REVIEWED_FIXTURE_DIGEST_VERIFIED": True, "REVISION": revision,
                "OBJECTIVE_PRESENT": True, "TRUSTED_TARGET_PRESENT": True,
                "TARGET_IN_VISIBLE_INPUT": current["target_in_visible_input"],
                "REMAINING_MODEL_CALLS": remaining,
                "REMAINING_SECONDS": seconds,
                "INDEPENDENT_ACCEPTANCE_PASSED": planning["task_acceptance_passed"],
                "CHOICE_SCHEMA_MATCHES_CATALOG": schema_ids == ids and
                    schema_revisions == [revision],
                "CANDIDATES": candidates, "VERIFIED_EFFECTS": effects}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BrowserRefusal("DIAGNOSTIC_INPUT_INVALID") from None
