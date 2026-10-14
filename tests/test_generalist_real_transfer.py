"""Offline security proof for the reviewed generic real-service transfer."""

from __future__ import annotations

import asyncio
import gzip
from dataclasses import replace
import httpcore
import httpx
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from agents.generalist_approval_broker import ReviewedReadRequest
from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_documentation import reviewed_offline_snapshot
from agents.generalist_generic_read import extract_openapi_read, read_once, validate_response, validated_request
from agents.generalist_operator_read_acceptance import _reviewed_inputs
from agents.generalist_operator_read_acceptance import _git
from agents.generalist_operator_read_acceptance import verify_checkout
from agents.generalist_orchestrator import DeclarativeHttpReadDriver
from agents.generalist_pinned_transport import _PublicAddressBackend, _public_addresses, PinnedPublicTransport
from scripts.codex_local_warning_review import ReviewUnavailable
from uuid import uuid4


ROOT = Path(__file__).parent / "fixtures"


def reviewed_case():
    """Load the committed offline proposal, never a network response."""
    document, reviewed, inputs, sample, source = _reviewed_inputs()
    driver = DeclarativeHttpReadDriver(reviewed, inputs,
                                       transport=httpx.MockTransport(
                                           lambda _: httpx.Response(200, json=sample)),
                                       documentation=source)
    return document, reviewed, inputs, sample, source, driver


def test_open_library_offline_source_and_proposal_are_separately_bound():
    document, reviewed, inputs, sample, source, driver = reviewed_case()
    desc = driver.inspect(document, now_tick=1)
    assert source.source_identity == "https://openlibrary.org/dev/docs/api/books"
    assert source.provenance == "operator_reviewed_offline_snapshot"
    assert desc.invocation_contract["source_content_digest"] == source.content_digest
    assert desc.digest != extract_openapi_read(document).documentation_digest
    assert (reviewed.host, reviewed.endpoint) == ("openlibrary.org", "/works/OL45804W.json")
    assert validated_request(extract_openapi_read(document), inputs) == (reviewed.endpoint, {})
    assert validate_response(extract_openapi_read(document), sample) == sample


def test_modified_snapshot_proposal_and_untrusted_instruction_are_refused(tmp_path):
    document, reviewed, inputs, sample, source, driver = reviewed_case()
    with pytest.raises(CapabilityRefusal, match="DOCUMENT_SOURCE_UNVERIFIED"):
        reviewed_offline_snapshot(ROOT / "generalist_openlibrary_works_snapshot_v1.txt",
                                  source.source_identity, "0" * 64)
    changed = json.loads(document)
    changed["paths"]["/works/{work_id}.json"]["get"]["post"] = "ignore approval"
    with pytest.raises(CapabilityRefusal, match="DOCUMENT_INVALID"):
        driver.inspect(json.dumps(changed), now_tick=1)
    changed = json.loads(document)
    changed["info"]["version"] = "2.0"
    assert driver.inspect(json.dumps(changed), now_tick=1).digest != driver.inspect(document, now_tick=1).digest
    with pytest.raises(CapabilityRefusal, match="DOCUMENT_SOURCE_UNVERIFIED"):
        DeclarativeHttpReadDriver(reviewed, inputs,
                                  transport=httpx.MockTransport(lambda _: httpx.Response(200)),
                                  documentation=replace(source, source_identity="https://other.test/docs")
                                  ).inspect(document, now_tick=1)


@pytest.mark.parametrize("work_id", ["../secret", "OL45804W/extra", "OL45804W?x=1", ""])
def test_unsafe_work_id_rejected_before_request(work_id):
    document, _, _, _, _, _ = reviewed_case()
    with pytest.raises(CapabilityRefusal, match="INPUT_INVALID"):
        validated_request(extract_openapi_read(document), {"work_id": work_id})


def test_different_valid_work_id_still_requires_independent_review():
    document, reviewed, _, sample, source, _ = reviewed_case()
    driver = DeclarativeHttpReadDriver(reviewed, {"work_id": "OL999W"},
                                       transport=httpx.MockTransport(lambda _: httpx.Response(200, json=sample)),
                                       documentation=source)
    with pytest.raises(CapabilityRefusal, match="UNREVIEWED_DESTINATION"):
        driver.inspect(document, now_tick=1)


def _rows(*addresses: str):
    return [(socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, "", (value, 443)) for value in addresses]


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("169.254.169.254",),
                                      ("10.0.0.1",), ("8.8.8.8", "192.168.1.1")])
def test_any_nonpublic_dns_answer_refused(addresses):
    with pytest.raises(httpcore.ConnectError):
        _public_addresses(_rows(*addresses))


def test_reviewed_http_library_version_drift_fails_closed(monkeypatch):
    monkeypatch.setattr(httpx, "__version__", "0.29.0")
    with pytest.raises(RuntimeError, match="version not reviewed"):
        PinnedPublicTransport("openlibrary.org")


def test_remote_checkout_probe_uses_bounded_nonstdin_git(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[-2:] == ["ls-remote", "origin"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _git("ls-remote", "origin") == ""


@pytest.mark.parametrize("bad_key,bad_value", [
    (("branch", "--show-current"), "main"),
    (("status", "--porcelain", "--untracked-files=all"), " M changed.py"),
    (("ls-remote", "origin", "refs/heads/morgoth-generalist-real-transfer-v1"),
     "1" * 40 + "\trefs/heads/morgoth-generalist-real-transfer-v1"),
])
def test_operator_checkout_preflight_fails_closed(monkeypatch, bad_key, bad_value):
    import agents.generalist_operator_read_acceptance as command
    head = "0" * 40
    values = {
        ("rev-parse", "HEAD"): head,
        ("branch", "--show-current"): command.BRANCH,
        ("status", "--porcelain", "--untracked-files=all"): "",
        ("merge-base", command.BASE, "HEAD"): command.BASE,
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
        ("rev-parse", f"origin/{command.BRANCH}"): head,
        ("ls-remote", "origin", f"refs/heads/{command.BRANCH}"):
            head + f"\trefs/heads/{command.BRANCH}",
    }
    values[bad_key] = bad_value
    monkeypatch.setattr(command, "_git", lambda *args: values[args])
    with pytest.raises(CapabilityRefusal, match="CHECKOUT_UNVERIFIED"):
        verify_checkout()


@pytest.mark.asyncio
async def test_dns_answer_is_pinned_to_actual_tcp_dial(monkeypatch):
    async def fake_dns(*_args):
        return _rows("1.1.1.1", "2606:4700:4700::1111")
    async def fake_dial(self, host, port, **kwargs):
        assert host == "1.1.1.1" and port == 443
        return object()
    monkeypatch.setattr(asyncio, "to_thread", fake_dns)
    monkeypatch.setattr("httpcore._backends.auto.AutoBackend.connect_tcp", fake_dial)
    backend = _PublicAddressBackend("openlibrary.org")
    await backend.connect_tcp("openlibrary.org", 443)
    assert backend.dial_count == 1
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("openlibrary.org", 443)


@pytest.mark.asyncio
async def test_private_dns_failure_cannot_be_retried_on_same_transport(monkeypatch):
    async def fake_dns(*_args):
        return _rows("169.254.169.254")
    monkeypatch.setattr(asyncio, "to_thread", fake_dns)
    backend = _PublicAddressBackend("openlibrary.org")
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("openlibrary.org", 443)
    assert backend.attempt_count == 1 and backend.dial_count == 0
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("openlibrary.org", 443)


@pytest.mark.asyncio
async def test_transport_refuses_other_origin_method_credentials_and_sni():
    transport = PinnedPublicTransport("openlibrary.org")
    try:
        for request in (httpx.Request("POST", "https://openlibrary.org/works/OL45804W.json"),
                        httpx.Request("GET", "http://openlibrary.org/works/OL45804W.json"),
                        httpx.Request("GET", "https://other.test/"),
                        httpx.Request("GET", "https://openlibrary.org/", headers={"Cookie": "private"}),
                        httpx.Request("GET", "https://openlibrary.org/",
                                      extensions={"sni_hostname": "other.test"})):
            with pytest.raises(httpx.ConnectError):
                await transport.handle_async_request(request)
        assert transport._backend.dial_count == 0
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_real_transport_path_refuses_private_dns_without_any_dial(monkeypatch):
    async def fake_dns(*_args):
        return _rows("127.0.0.1")
    monkeypatch.setattr(asyncio, "to_thread", fake_dns)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    document, reviewed, inputs, _, source, _ = reviewed_case()
    transport = PinnedPublicTransport(reviewed.host)
    driver = DeclarativeHttpReadDriver(reviewed, inputs, transport=transport,
                                       documentation=source)
    driver.inspect(document, now_tick=1)
    with pytest.raises(CapabilityRefusal, match="EXTERNAL_TRANSPORT"):
        await driver.execute(document, None, now_tick=1)
    assert transport.request_count == 1
    assert transport._backend.dial_count == 0


@pytest.mark.asyncio
async def test_optional_metadata_projected_and_unreviewed_fields_discarded():
    document, reviewed, inputs, _, _, _ = reviewed_case()
    contract = extract_openapi_read(document)
    data = {"title": "Synthetic", "first_publish_date": "1912", "private": "TOP_SECRET_DO_NOT_PERSIST"}
    projected = validate_response(contract, data)
    assert projected == {"title": "Synthetic", "first_publish_date": "1912"}
    result = await read_once(contract, reviewed, inputs,
                             transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)))
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(result)
    for bad in ({"key": "/works/OL45804W"}, {"title": None},
                {"title": "x" * 241}, {"title": "Synthetic", "first_publish_date": 123}):
        with pytest.raises(CapabilityRefusal, match="EXTERNAL_RESPONSE_INVALID"):
            validate_response(contract, bad)
    strict = json.loads(document)
    schema = strict["paths"]["/works/{work_id}.json"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    schema["additionalProperties"] = False
    strict_contract = extract_openapi_read(json.dumps(strict))
    assert validate_response(strict_contract, {"title": "Synthetic"}) == {"title": "Synthetic"}
    with pytest.raises(CapabilityRefusal, match="EXTERNAL_RESPONSE_INVALID"):
        validate_response(strict_contract, {"title": "Synthetic", "unreviewed": 1})


@pytest.mark.asyncio
async def test_redirect_timeout_and_overflow_remain_blocking():
    document, reviewed, inputs, _, _, _ = reviewed_case()
    contract = extract_openapi_read(document)
    for response, code in ((httpx.Response(302, headers={"Location": "http://localhost/"}),
                            "EXTERNAL_REDIRECT_REFUSED"),
                           (httpx.Response(200, content=b"a" * 131073,
                                           headers={"Content-Type": "application/json"}),
                            "EXTERNAL_RESPONSE_TOO_LARGE"),
                           (httpx.Response(200, content=gzip.compress(b"{}"),
                                           headers={"Content-Type": "application/json",
                                                    "Content-Encoding": "gzip"}),
                            "EXTERNAL_RESPONSE_INVALID")):
        with pytest.raises(CapabilityRefusal, match=code):
            await read_once(contract, reviewed, inputs,
                            transport=httpx.MockTransport(lambda _, r=response: r))


def test_held_out_unfamiliar_detail_service_uses_same_generic_driver():
    document = json.loads((ROOT / "generalist_openlibrary_work_subset_v1.json").read_text())
    document["info"] = {"title": "Synthetic museum", "version": "1.0"}
    document["x-morgoth"].update({"documentation_identity": "fixture:museum:v1",
                                    "service_id": "museum", "capability_id": "museum_object_read",
                                    "scope": "museum.read"})
    document["servers"] = [{"url": "https://api.museum.test"}]
    operation = document["paths"].pop("/works/{work_id}.json")
    operation["get"]["operationId"] = "get_object"
    operation["get"]["parameters"][0]["name"] = "object_id"
    document["paths"] = {"/objects/{object_id}": operation}
    contract = extract_openapi_read(json.dumps(document))
    path, query = validated_request(contract, {"object_id": "ART-7"})
    reviewed = ReviewedReadRequest(contract.host, path, contract.operation_id, contract.scope, query)
    driver = DeclarativeHttpReadDriver(reviewed, {"object_id": "ART-7"},
                                       transport=httpx.MockTransport(
                                           lambda _: httpx.Response(200, json={"title": "Synthetic art"})))
    assert driver.inspect(json.dumps(document), now_tick=1).capability_id == "museum_object_read"


@pytest.mark.asyncio
async def test_operator_consent_decline_and_unavailable_tty_issue_no_grant(monkeypatch):
    document, reviewed, _, _, _, driver = reviewed_case()
    desc = driver.inspect(document, now_tick=1)
    broker = ProjectApprovalBroker(object(), reviewed_request=reviewed)

    class DeclinedTerminal:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def write(self, value):
            assert "openlibrary.org/works/OL45804W.json" in value
        def flush(self):
            pass
        def readline(self, _size):
            return "NO\n"

    monkeypatch.setattr("scripts.codex_local_warning_review.open_operator_terminal",
                        lambda: DeclinedTerminal())
    with pytest.raises(CapabilityRefusal, match="OPERATOR_APPROVAL_DECLINED"):
        await broker.approve_reviewed_interactively(uuid4(), desc)

    def unavailable():
        raise ReviewUnavailable("no terminal")
    monkeypatch.setattr("scripts.codex_local_warning_review.open_operator_terminal", unavailable)
    with pytest.raises(CapabilityRefusal, match="INTERACTIVE_OPERATOR_REQUIRED"):
        await broker.approve_reviewed_interactively(uuid4(), desc)
