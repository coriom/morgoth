"""LLM-driven Python module generation for Morgoth."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig, PermissionDeniedError
from core.llm_client import ChatMessage, OllamaLLMClient


class CodeGenerationRequest(BaseModel):
    """Code generation input payload."""

    module_path: str
    specification: str
    context: list[str] = Field(default_factory=list)
    task_type: str = "self_modify"


class GeneratedModule(BaseModel):
    """Generated module output."""

    module_path: str
    content: str
    model: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CodeWriter:
    """Generate Python modules inside the permitted evolvable zone."""

    def __init__(self, config: AppConfig, llm_client: OllamaLLMClient) -> None:
        """Store runtime dependencies."""

        self._config = config
        self._llm_client = llm_client

    async def generate_module(self, request: CodeGenerationRequest) -> GeneratedModule:
        """Generate Python source code for the requested module."""

        self._ensure_self_modify_enabled()
        target_path = self._config.ensure_path_writable(request.module_path)
        if target_path.suffix != ".py":
            raise ValueError(f"Generated module must be a Python file: {target_path}")

        model = self._config.choose_model_for_task(request.task_type)
        response = await self._llm_client.chat(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "You write production Python 3.11 modules for Morgoth. "
                        "Return only valid Python source code with docstrings and type hints. "
                        "Do not wrap the answer in Markdown fences."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=self._build_prompt(target_path, request),
                ),
            ],
            model=model,
            options={"temperature": 0.1},
        )
        content = self._strip_code_fences(response.message.content or "")
        self._validate_python(target_path, content)
        logger.info("Generated module for '{}'", target_path)
        return GeneratedModule(
            module_path=str(target_path.relative_to(self._config.root_dir)),
            content=content,
            model=model,
        )

    def _build_prompt(self, target_path: Path, request: CodeGenerationRequest) -> str:
        """Build the generation prompt for the LLM."""

        context_block = "\n\n".join(request.context).strip() or "No additional context supplied."
        return (
            f"Target file: {target_path.relative_to(self._config.root_dir)}\n"
            "Constraints:\n"
            "- Python 3.11+\n"
            "- Async-safe where relevant\n"
            "- Use loguru for runtime logging\n"
            "- Include docstrings on public functions\n"
            "- Avoid placeholder comments and TODOs\n\n"
            f"Specification:\n{request.specification.strip()}\n\n"
            f"Context:\n{context_block}\n"
        )

    def _ensure_self_modify_enabled(self) -> None:
        """Fail fast when self-modification is disabled."""

        if not self._config.permissions.permissions.can_self_modify:
            raise PermissionDeniedError("Self-modification is disabled by permissions")

    def _validate_python(self, target_path: Path, content: str) -> None:
        """Ensure the generated source compiles before returning it."""

        try:
            compile(content, str(target_path), "exec")
        except SyntaxError:
            logger.exception("Generated Python failed syntax validation for '{}'", target_path)
            raise

    def _strip_code_fences(self, content: str) -> str:
        """Normalize LLM output to plain Python source."""

        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned + ("\n" if cleaned and not cleaned.endswith("\n") else "")
