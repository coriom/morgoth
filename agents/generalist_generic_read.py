"""Opt-in, deliberately small OpenAPI 3.0 GET interpreter.

Documents describe data. A separate reviewed request binds an exact host,
concrete path, parameters and scope before the disposable approval broker runs.
This module is not registered with the autonomous tool router.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from agents.generalist_approval_broker import ReviewedReadRequest
from agents.generalist_capability import CapabilityRefusal, _unique_pairs


_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MAX_BODY = 131072


def _valid_host(host: str) -> bool:
    labels = host.split(".")
    return (2 <= len(labels) <= 8 and len(host) <= 253
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels)
            and not host.replace(".", "").isdigit()
            and host != "localhost" and not host.endswith(".localhost"))


def _documentation_identity(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 160:
        raise ValueError("document identity")
    if re.fullmatch(r"fixture:[a-z][a-z0-9_]{0,31}:v[1-9][0-9]*", value):
        return value
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port is not None or not parsed.hostname
            or not _valid_host(parsed.hostname) or parsed.query or parsed.fragment
            or re.fullmatch(r"/[A-Za-z0-9_./-]{1,120}", parsed.path) is None
            or ".." in parsed.path):
        raise ValueError("document identity")
    return value


def _keys(value: object, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("unsupported schema")
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError("invalid identifier")
    return value


def _field_schema(value: object) -> dict[str, Any]:
    if type(value) is not dict or value.get("type") not in {"string", "integer", "boolean"}:
        raise ValueError("unsupported field")
    kind = value["type"]
    if kind == "string":
        _keys(value, {"type", "maxLength"})
        if type(value["maxLength"]) is not int or not 1 <= value["maxLength"] <= 240:
            raise ValueError("string bound")
    elif kind == "integer":
        _keys(value, {"type", "minimum", "maximum"})
        if (type(value["minimum"]) is not int or type(value["maximum"]) is not int
                or not 0 <= value["minimum"] <= value["maximum"] <= 1000000):
            raise ValueError("integer bound")
    else:
        _keys(value, {"type"})
    return value


def _object_schema(value: object) -> dict[str, Any]:
    raw = _keys(value, {"type", "additionalProperties", "required", "properties"})
    if raw["type"] != "object" or type(raw["additionalProperties"]) is not bool:
        raise ValueError("object required")
    fields = raw["properties"]
    if (type(fields) is not dict or not 1 <= len(fields) <= 12
            or type(raw["required"]) is not list or set(raw["required"]) != set(fields)
            or len(raw["required"]) != len(fields)):
        raise ValueError("required fields")
    for name, schema in fields.items():
        _identifier(name)
        _field_schema(schema)
    return raw


@dataclass(frozen=True)
class LearnedReadContract:
    """Data-only operation identity; this object itself confers no authority."""

    documentation_identity: str
    documentation_digest: str
    service_id: str
    capability_id: str
    version: str
    operation_id: str
    scope: str
    host: str
    path_template: str
    parameters: tuple[dict[str, Any], ...]
    response_schema: dict[str, Any]
    shape: str
    valid_until_tick: int


def extract_openapi_read(document: str) -> LearnedReadContract:
    """Extract exactly one collection or detail GET from a bounded OpenAPI subset."""
    if len(document.encode("utf-8")) > 16384:
        raise CapabilityRefusal("DOCUMENT_INVALID")
    try:
        raw = json.loads(document, object_pairs_hook=_unique_pairs)
        _keys(raw, {"openapi", "info", "x-morgoth", "servers", "paths"})
        if raw["openapi"] != "3.0.3":
            raise ValueError("version")
        info = _keys(raw["info"], {"title", "version"})
        if type(info["title"]) is not str or not 1 <= len(info["title"]) <= 80:
            raise ValueError("title")
        version = info["version"]
        if type(version) is not str or re.fullmatch(r"[1-9][0-9]*\.[0-9]+", version) is None:
            raise ValueError("version")
        meta = _keys(raw["x-morgoth"], {"documentation_identity", "service_id",
                                         "capability_id", "scope", "valid_until_tick"})
        doc_id = _documentation_identity(meta["documentation_identity"])
        service, capability = _identifier(meta["service_id"]), _identifier(meta["capability_id"])
        scope = meta["scope"]
        if type(scope) is not str or re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", scope) is None:
            raise ValueError("scope")
        tick = meta["valid_until_tick"]
        if type(tick) is not int or not 1 <= tick <= 1000000:
            raise ValueError("staleness")
        servers = raw["servers"]
        if type(servers) is not list or len(servers) != 1:
            raise ValueError("server count")
        server = _keys(servers[0], {"url"})["url"]
        if type(server) is not str or not server.startswith("https://"):
            raise ValueError("server")
        host = server[8:]
        if _HOST.fullmatch(host) is None or not _valid_host(host):
            raise ValueError("host")
        paths = raw["paths"]
        if type(paths) is not dict or len(paths) != 1:
            raise ValueError("path count")
        path, item = next(iter(paths.items()))
        if (type(path) is not str or len(path) > 120 or not path.startswith("/")
                or "//" in path or ".." in path or "%" in path
                or any(c in path for c in "?#\\") or "." in path.split("/")
                or re.fullmatch(r"/[A-Za-z0-9_/{}/.-]+", path) is None):
            raise ValueError("path")
        op = _keys(item, {"get"})["get"]
        _keys(op, {"operationId", "parameters", "responses"})
        operation = _identifier(op["operationId"])
        params = op["parameters"]
        if type(params) is not list or len(params) > 6:
            raise ValueError("parameters")
        normalized = []
        for param in params:
            _keys(param, {"name", "in", "required", "schema"})
            name = _identifier(param["name"])
            if param["in"] not in {"path", "query"} or param["required"] is not True:
                raise ValueError("parameter authority")
            schema = _field_schema(param["schema"])
            normalized.append({"name": name, "in": param["in"], "schema": schema})
        names = [p["name"] for p in normalized]
        if len(set(names)) != len(names):
            raise ValueError("duplicate parameter")
        placeholders = re.findall(r"\{([^{}]+)\}", path)
        if path.count("{") != len(placeholders) or path.count("}") != len(placeholders):
            raise ValueError("placeholder")
        if set(placeholders) != {p["name"] for p in normalized if p["in"] == "path"}:
            raise ValueError("path parameter")
        shape = "detail" if placeholders else "collection"
        responses = _keys(op["responses"], {"200"})
        content = _keys(responses["200"], {"content"})["content"]
        schema = _keys(_keys(content, {"application/json"})["application/json"], {"schema"})["schema"]
        if shape == "collection":
            _keys(schema, {"type", "maxItems", "items"})
            if schema["type"] != "array" or type(schema["maxItems"]) is not int or not 1 <= schema["maxItems"] <= 10:
                raise ValueError("array bound")
            _object_schema(schema["items"])
        else:
            _object_schema(schema)
        return LearnedReadContract(doc_id, sha256(document.encode()).hexdigest(), service,
                                   capability, version, operation, scope, host, path,
                                   tuple(normalized), schema, shape, tick)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CapabilityRefusal("DOCUMENT_INVALID") from None


def _checked_value(value: object, schema: dict[str, Any], *, path: bool = False) -> str | int | bool:
    kind = schema["type"]
    if kind == "string":
        if (type(value) is not str or not 1 <= len(value) <= schema["maxLength"]
                or any(ord(c) < 32 for c in value)):
            raise CapabilityRefusal("INPUT_INVALID")
        if path and (_SEGMENT.fullmatch(value) is None or value in {".", ".."} or value.endswith(".")):
            raise CapabilityRefusal("INPUT_INVALID")
    elif kind == "integer":
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise CapabilityRefusal("INPUT_INVALID")
    elif type(value) is not bool:
        raise CapabilityRefusal("INPUT_INVALID")
    return value


def validated_request(contract: LearnedReadContract, inputs: dict[str, object]) -> tuple[str, dict[str, str | int | bool]]:
    """Validate typed inputs and render a concrete path, never a URL."""
    if type(inputs) is not dict or set(inputs) != {p["name"] for p in contract.parameters}:
        raise CapabilityRefusal("INPUT_INVALID")
    path, query = contract.path_template, {}
    for param in contract.parameters:
        name = param["name"]
        value = _checked_value(inputs[name], param["schema"], path=param["in"] == "path")
        if param["in"] == "path":
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        else:
            query[name] = value
    return path, query


def _validate_object(value: object, schema: dict[str, Any]) -> dict[str, object]:
    if type(value) is not dict or not set(schema["required"]).issubset(value):
        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
    if schema["additionalProperties"] is False and set(value) != set(schema["properties"]):
        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
    # Extra response fields are discarded, never interpreted as instructions.
    try:
        return {key: _checked_value(value[key], field) for key, field in schema["properties"].items()}
    except CapabilityRefusal:
        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID") from None


def validate_response(contract: LearnedReadContract, value: object) -> object:
    """Project a bounded typed result from a JSON response."""
    if contract.shape == "collection":
        if type(value) is not list or len(value) > contract.response_schema["maxItems"]:
            raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
        return [_validate_object(row, contract.response_schema["items"]) for row in value]
    return _validate_object(value, contract.response_schema)


async def read_once(contract: LearnedReadContract, reviewed: ReviewedReadRequest,
                    inputs: dict[str, object], *,
                    transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Make at most one HTTPS GET with no inherited proxy, cookies or redirects."""
    path, query = validated_request(contract, inputs)
    if (type(reviewed) is not ReviewedReadRequest or
            (contract.host, path, contract.operation_id, contract.scope, query) !=
            (reviewed.host, reviewed.endpoint, reviewed.operation,
             reviewed.scope, reviewed.query)):
        raise CapabilityRefusal("UNREVIEWED_DESTINATION")
    if type(transport) is not httpx.MockTransport:
        raise CapabilityRefusal("GENERIC_LIVE_READ_NOT_AUTHORIZED")
    async def request() -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(transport=transport, trust_env=False,
                                         follow_redirects=False,
                                         timeout=httpx.Timeout(8, connect=3, read=3),
                                         headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                                  "User-Agent": "Morgoth-disposable-read"}) as client:
                async with client.stream("GET", f"https://{contract.host}{path}", params=query) as response:
                    status = response.status_code
                    if status in {401, 403, 404, 429}:
                        raise CapabilityRefusal(f"EXTERNAL_HTTP_{status}")
                    if 300 <= status < 400:
                        raise CapabilityRefusal("EXTERNAL_REDIRECT_REFUSED")
                    if status >= 500:
                        raise CapabilityRefusal("EXTERNAL_HTTP_5XX")
                    if status != 200:
                        raise CapabilityRefusal("EXTERNAL_HTTP_OTHER")
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_BODY:
                            raise CapabilityRefusal("EXTERNAL_RESPONSE_TOO_LARGE")
        except httpx.TimeoutException:
            raise CapabilityRefusal("EXTERNAL_TIMEOUT") from None
        except httpx.RequestError:
            raise CapabilityRefusal("EXTERNAL_TRANSPORT") from None
        try:
            decoded = json.loads(body, object_pairs_hook=_unique_pairs)
            result = validate_response(contract, decoded)
        except (ValueError, UnicodeDecodeError):
            raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID") from None
        return {"source_identity": contract.documentation_identity,
                "operation": contract.operation_id,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "response_sha256": sha256(body).hexdigest(), "result": result}
    try:
        return await asyncio.wait_for(request(), timeout=8)
    except TimeoutError:
        raise CapabilityRefusal("EXTERNAL_TIMEOUT") from None
