"""Real checkout-gate logic with a controlled Git subprocess boundary."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agents import generalist_browser_operator as operator
from agents import generalist_compact_model_qualification as compact
from agents import generalist_mission_qualification_cmq4 as campaign


SHA = "a" * 40
BRANCH = campaign.BRANCH


def fake_git_run(*, change: str = "", calls: list[str] | None = None):
    """Return only reviewed Git metadata or one fixed simulated failure."""
    def run(command, **kwargs):
        args = tuple(command[3:])
        operation = operator._git_operation(args)
        if calls is not None:
            calls.append(operation)
        assert kwargs["timeout"] == 8
        assert kwargs["capture_output"] is True
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        if change == "timeout" and operation == "REMOTE_HEAD":
            raise subprocess.TimeoutExpired(command, 8, output="PRIVATE_DATA")
        if change == "exit" and operation == "REMOTE_HEAD":
            return SimpleNamespace(returncode=128, stdout="", stderr="PRIVATE_DATA")
        values = {
            "REMOTE_URL": ("git@github.com:unknown/repo.git" if change == "origin" else
                           "git@github.com:coriom/morgoth.git") + "\n",
            "BRANCH": ("wrong-branch" if change == "branch" else BRANCH) + "\n",
            "LOCAL_HEAD": ("b" * 40 if change == "local" else SHA) + "\n",
            "TRACKING_HEAD": SHA + "\n",
            "WORKTREE_STATUS": " M untracked\n" if change == "dirty" else "",
            "REMOTE_HEAD": ("b" * 40 if change == "remote" else SHA) +
                           f"\trefs/heads/{BRANCH}\n",
        }
        return SimpleNamespace(returncode=0, stdout=values[operation], stderr="")
    return run


@pytest.mark.asyncio
async def test_git_timeout_is_same_fixed_refusal_in_both_gates_and_preflights(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(operator.subprocess, "run", fake_git_run(change="timeout", calls=calls))
    for check in (lambda: operator._checkout_verified(BRANCH),
                  lambda: compact.verify_checkout(SHA, BRANCH)):
        with pytest.raises(operator.GitCheckoutRefusal) as error:
            check()
        assert error.value.code == "PREFLIGHT_CHECKOUT"
        assert (error.value.operation, error.value.failure) == ("REMOTE_HEAD", "TIMEOUT")
        assert "PRIVATE_DATA" not in str(error.value)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("model or browser accessed")
    monkeypatch.setattr(compact, "_local_model_config", forbidden)
    monkeypatch.setattr(operator, "_sandbox_command", forbidden)
    preflight, config = await compact._preflight("qwen3:4b-instruct", SHA, "Q4_K_M", BRANCH)
    assert config is None
    assert preflight["FAILURE_CLASS"] == "PREFLIGHT_CHECKOUT"
    assert (preflight["CHECKOUT_OPERATION"], preflight["CHECKOUT_FAILURE"]) == (
        "REMOTE_HEAD", "TIMEOUT")
    browser = await operator._evaluate(branch=BRANCH)
    assert browser["FAILURE_CLASS"] == "PREFLIGHT_CHECKOUT"
    assert browser["MODEL_CALL_COUNT"] == 0
    assert browser["CHECKOUT_FAILURE"] == "TIMEOUT"
    assert calls.count("REMOTE_HEAD") == 4


@pytest.mark.parametrize("change,operation,failure", [
    ("exit", "REMOTE_HEAD", "EXIT_NONZERO"),
    ("origin", "REMOTE_URL", "MISMATCH"),
    ("branch", "BRANCH", "MISMATCH"),
    ("dirty", "WORKTREE_STATUS", "DIRTY"),
    ("remote", "REMOTE_HEAD", "MISMATCH"),
])
def test_both_gates_keep_exact_refusals(monkeypatch, change, operation, failure) -> None:
    monkeypatch.setattr(operator.subprocess, "run", fake_git_run(change=change))
    for check in (lambda: operator._checkout_verified(BRANCH),
                  lambda: compact.verify_checkout(SHA, BRANCH)):
        with pytest.raises(operator.GitCheckoutRefusal) as error:
            check()
        assert (error.value.code, error.value.operation, error.value.failure) == (
            "PREFLIGHT_CHECKOUT", operation, failure)


def test_compact_gate_refuses_wrong_expected_sha_and_tracking_ref(monkeypatch) -> None:
    monkeypatch.setattr(operator.subprocess, "run", fake_git_run())
    with pytest.raises(operator.GitCheckoutRefusal) as error:
        compact.verify_checkout("b" * 40, BRANCH)
    assert (error.value.operation, error.value.failure) == ("LOCAL_HEAD", "MISMATCH")
    monkeypatch.setattr(operator.subprocess, "run", lambda command, **kwargs: (
        SimpleNamespace(returncode=0, stdout="b" * 40 + "\n", stderr="")
        if operator._git_operation(tuple(command[3:])) == "TRACKING_HEAD" else
        fake_git_run()(command, **kwargs)))
    with pytest.raises(operator.GitCheckoutRefusal) as error:
        compact.verify_checkout(SHA, BRANCH)
    assert (error.value.operation, error.value.failure) == ("TRACKING_HEAD", "MISMATCH")


def test_diagnostic_calls_real_gates_without_generation(monkeypatch) -> None:
    monkeypatch.setattr(operator.subprocess, "run", fake_git_run(change="timeout"))
    monkeypatch.setattr(campaign, "_choice_probe", lambda *_args: (_ for _ in ()).throw(
        AssertionError("inference attempted")))
    result = campaign.diagnose_checkout(SHA)
    assert result["VERDICT"] == "CHECKOUT_GATES_BLOCKED"
    assert result["INFERENCE_ATTEMPTED"] is False
    assert result["MODULE_ROOT_CONSISTENT"] is True
    assert all(item["STATUS"] == "REFUSED" and item["OPERATION"] == "REMOTE_HEAD"
               and item["FAILURE"] == "TIMEOUT" and 0 <= item["ELAPSED_MS"] <= 120000
               for item in result["GATES"].values())
    assert "PRIVATE_DATA" not in str(result)


@pytest.mark.asyncio
async def test_consumed_campaign_cannot_run_from_correction_branch(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("checkout or inference attempted")
    monkeypatch.setattr(compact, "verify_checkout", forbidden)
    result = await campaign.evaluate(SHA, run=True, recover=True)
    assert result["FAILURE_CLASS"] == "CAMPAIGN_ALREADY_CONSUMED"
    assert result["REQUESTS_DISPATCHED"] == 0
    assert campaign.inspect_recovery(SHA)["FAILURE_CLASS"] == "CAMPAIGN_ALREADY_CONSUMED"


def test_diagnostic_cli_uses_only_checkout_reads(monkeypatch, capsys) -> None:
    calls: list[str] = []
    monkeypatch.setattr(operator.subprocess, "run", fake_git_run(calls=calls))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("model or ledger accessed")
    monkeypatch.setattr(campaign, "_local_model_config", forbidden)
    monkeypatch.setattr(campaign, "_inspect_original_ledger", forbidden)
    monkeypatch.setattr(campaign.sys, "argv", ["cmq4", "--diagnose-checkout",
                                              "--expected-sha", SHA])
    campaign.main()
    import json
    result = json.loads(capsys.readouterr().out)
    assert result["VERDICT"] == "CHECKOUT_GATES_PASSED"
    assert result["INFERENCE_ATTEMPTED"] is False
    assert calls.count("REMOTE_HEAD") == 2
