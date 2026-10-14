"""Opt-in, data-only GitHub public issue reader; never registered as a runtime tool.

Only the exact reviewed host and endpoint shape are executable. Documentation
data names a capability but cannot add hosts, methods, headers or authority.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import re
import socket
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents.generalist_capability import CapabilityRefusal, _unique_pairs


_DOC_URI = "https://docs.github.com/en/rest/issues/issues#list-repository-issues"
_HOST = "api.github.com"
_MAX_BYTES = 131072


class PublicIssuesContract(BaseModel):
    """One manually extracted, constrained subset of the GitHub Issues API."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    service_id: Literal["public_github_issues"]
    version: str = Field(pattern=r"^[1-9][0-9]*\.[0-9]+$")
    schema_revision: Literal[1]
    documentation_identity: Literal[_DOC_URI]
    method: Literal["GET"]
    host: Literal[_HOST]
    path_template: Literal["/repos/{owner}/{repo}/issues"]
    operation_id: Literal["list_open_issues"]
    scope: Literal["public_issues.read"]
    state: Literal["open"]
    per_page: int = Field(ge=1, le=3)
    owner: Literal["octocat"]
    repo: Literal["Hello-World"]
    result_fields: list[str] = Field(min_length=5, max_length=5)
    valid_until_tick: int = Field(ge=1, le=100000)

    @model_validator(mode="after")
    def exact_shape(self) -> "PublicIssuesContract":
        """Reject path tricks and arbitrary result schemas."""
        if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", self.version) is None:
            raise ValueError("invalid contract version")
        for component in (self.owner, self.repo):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,38}", component):
                raise ValueError("invalid repository component")
            if component in {".", ".."} or component.endswith("."):
                raise ValueError("invalid repository component")
        if self.result_fields != ["number", "title", "state", "created_at", "updated_at"]:
            raise ValueError("unsupported result shape")
        return self


def parse_public_issues_contract(text: str, *, now_tick: int) -> tuple[PublicIssuesContract, str]:
    """Parse untrusted data, refusing unknown keys, duplicates and stale versions."""
    if len(text.encode("utf-8")) > 4096:
        raise CapabilityRefusal("DOCUMENT_INVALID")
    try:
        raw = json.loads(text, object_pairs_hook=_unique_pairs)
        contract = PublicIssuesContract.model_validate(raw)
    except (ValueError, TypeError, ValidationError):
        raise CapabilityRefusal("DOCUMENT_INVALID") from None
    if now_tick > contract.valid_until_tick:
        raise CapabilityRefusal("DOCUMENT_STALE")
    return contract, sha256(text.encode("utf-8")).hexdigest()


def validate_issue_rows(raw: object) -> list[dict[str, str | int]]:
    """Extract only bounded, evidenced fields; PR entries are not issues."""
    if not isinstance(raw, list) or len(raw) > 3:
        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
    result: list[dict[str, str | int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
        if "pull_request" in item:
            continue
        number, title, state = item.get("number"), item.get("title"), item.get("state")
        if (type(number) is not int or number <= 0 or not isinstance(title, str) or
            not (1 <= len(title) <= 240) or any(ord(char) < 32 for char in title) or
            state != "open"):
            raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
        row: dict[str, str | int] = {"number": number, "title": title, "state": state}
        for key in ("created_at", "updated_at"):
            value = item.get(key)
            if not isinstance(value, str) or len(value) > 40:
                raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID") from None
            if parsed.tzinfo is None:
                raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
            row[key] = value
        result.append(row)
    if len({row["number"] for row in result}) != len(result):
        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
    return result


def validate_isolated_public_issues(contract: PublicIssuesContract, sample: object) -> str:
    """Validate a mocked response; this is not proof of live connectivity."""
    rows = validate_issue_rows(sample)
    if contract.result_fields != ["number", "title", "state", "created_at", "updated_at"]:
        raise CapabilityRefusal("DOCUMENT_INVALID")
    return sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _resolve_public_host() -> None:
    """Reject private DNS answers as defense in depth for the fixed TLS host."""
    try:
        addresses = socket.getaddrinfo(_HOST, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise CapabilityRefusal("EXTERNAL_DNS_UNAVAILABLE")
        if any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise CapabilityRefusal("PRIVATE_NETWORK_TARGET")
    except OSError:
        raise CapabilityRefusal("EXTERNAL_DNS_UNAVAILABLE") from None


async def _read_public_issues_once(contract: PublicIssuesContract, *,
                                   transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Perform one credential-free HTTPS GET with bounded body and no redirects."""
    if transport is None:
        await asyncio.to_thread(_resolve_public_host)
    url = f"https://{_HOST}/repos/{contract.owner}/{contract.repo}/issues"
    headers = {"Accept": "application/vnd.github+json", "Accept-Encoding": "identity",
               "User-Agent": "Morgoth-public-read-proof"}
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(8.0, connect=3.0, read=3.0), headers=headers,
                                     max_redirects=0) as client:
            async with client.stream("GET", url,
                                     params={"state": "open", "per_page": contract.per_page}) as response:
                if response.status_code in {401, 403, 404, 429}:
                    raise CapabilityRefusal(f"EXTERNAL_HTTP_{response.status_code}")
                if 300 <= response.status_code < 400:
                    raise CapabilityRefusal("EXTERNAL_REDIRECT_REFUSED")
                if response.status_code >= 500:
                    raise CapabilityRefusal("EXTERNAL_HTTP_5XX")
                if response.status_code != 200:
                    raise CapabilityRefusal("EXTERNAL_HTTP_OTHER")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        raise CapabilityRefusal("EXTERNAL_RESPONSE_TOO_LARGE")
    except httpx.TimeoutException:
        raise CapabilityRefusal("EXTERNAL_TIMEOUT") from None
    except httpx.RequestError:
        raise CapabilityRefusal("EXTERNAL_TRANSPORT") from None
    try:
        rows = validate_issue_rows(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID") from None
    return {"source_identity": f"github:{contract.owner}/{contract.repo}",
            "operation": contract.operation_id,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "response_sha256": sha256(body).hexdigest(), "issue_count": len(rows), "issues": rows}


async def read_public_issues(contract: PublicIssuesContract, *,
                             transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Apply an eight-second total deadline to DNS, connection and body read."""
    try:
        return await asyncio.wait_for(_read_public_issues_once(contract, transport=transport), timeout=8)
    except TimeoutError:
        raise CapabilityRefusal("EXTERNAL_TIMEOUT") from None
