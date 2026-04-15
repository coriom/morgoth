"""Unit tests for Phase 2 self-modification components."""

from __future__ import annotations

import pytest

from core.config import PermissionDeniedError
from self_modify.code_tester import CodeTester, TestRunRequest
from self_modify.code_writer import CodeGenerationRequest, CodeWriter


pytestmark = pytest.mark.asyncio


class DummyLLMClient:
    """Simple LLM client double returning fixed code."""

    async def chat(self, messages, model=None, options=None):  # type: ignore[no-untyped-def]
        class _Message:
            content = "```python\nasync def run() -> str:\n    return 'ok'\n```"

        class _Response:
            message = _Message()

        return _Response()


async def test_code_writer_requires_self_modify_permission(app_config) -> None:
    """Code writer should respect the self-modify permission gate."""

    app_config.permissions.permissions.can_self_modify = False
    writer = CodeWriter(app_config, DummyLLMClient())
    with pytest.raises(PermissionDeniedError):
        await writer.generate_module(
            CodeGenerationRequest(
                module_path="tools/generated_tool.py",
                specification="Create a tool",
            )
        )


async def test_code_writer_strips_fences_and_validates_python(app_config) -> None:
    """Code writer should return plain Python source."""

    app_config.permissions.permissions.can_self_modify = True
    writer = CodeWriter(app_config, DummyLLMClient())
    result = await writer.generate_module(
        CodeGenerationRequest(
            module_path="tools/generated_tool.py",
            specification="Create a module with one async function.",
        )
    )
    assert result.content.startswith("async def run")
    assert result.content.endswith("\n")


async def test_code_tester_runs_pytest_successfully(app_config) -> None:
    """Code tester should execute pytest against a repo-local target."""

    test_file = app_config.root_dir / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_generated() -> None:\n    assert 1 + 1 == 2\n", encoding="utf-8")

    tester = CodeTester(app_config)
    result = await tester.run_pytest(TestRunRequest(paths=["tests/test_generated.py"], timeout_seconds=30))
    assert result.success is True
