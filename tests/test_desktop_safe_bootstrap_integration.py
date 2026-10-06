"""Actual launcher PAUSED smoke against a disposable morgoth_test database."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest

from core.project import load_projects
from core.runtime import RuntimeHome
from project_manager import ProjectManager


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _LocalOllama(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self) -> None:
        self.requests.append(self.path)
        if self.path != "/api/tags":
            self.send_error(404)
            return
        body = json.dumps({"models": [{"name": "test-primary"}, {"name": "test-agent"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Never put test HTTP traffic or headers in test output."""


def _wait_paused(child: subprocess.Popen[bytes], port: int, token_path: Path) -> dict:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        assert child.poll() is None, "research launcher exited before PAUSED"
        if token_path.is_file():
            token = token_path.read_text(encoding="utf-8").strip()
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{port}/api/runtime/v1/status",
                    headers={"X-Morgoth-Token": token}, timeout=2, trust_env=False,
                )
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
        time.sleep(0.2)
    pytest.fail("research launcher did not reach authenticated PAUSED state in time")


def _stop_exact_child(child: subprocess.Popen[bytes]) -> None:
    """Terminate and reap only the disposable launcher created by this test."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.integration
def test_actual_crypto_and_weather_launch_paused_without_research(tmp_path: Path) -> None:
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("crypto_lab", name="Crypto lab", domain="crypto")
    manager.create_project("weather_lab", name="Weather lab", domain="weather")
    projects = {p.id: p for p in load_projects(home / "projects")}
    _LocalOllama.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    repo = Path(__file__).resolve().parents[1]
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8",
        "PYTHONPATH": str(repo), "POSTGRES_URL": dsn,
        "OLLAMA_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "OLLAMA_PRIMARY_MODEL": "test-primary", "OLLAMA_AGENT_MODEL": "test-agent",
        "SECRET_KEY": "synthetic-disposable-key", "MAX_CONCURRENT_AGENTS": "1",
        "LOG_RETENTION_DAYS": "1", "LOG_LEVEL_THOUGHT": "false",
    }
    children: dict[str, subprocess.Popen[bytes]] = {}
    try:
        for identifier in ("crypto_lab", "weather_lab"):
            project = projects[identifier]
            port = _free_loopback_port()
            argv = [sys.executable, "-B", "-m", "scripts.research_engine",
                    "--home", str(home), "--project", identifier, "--port", str(port)]
            child = subprocess.Popen(argv, cwd=repo, env=environment,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children[identifier] = child
            status = _wait_paused(child, port, project.ui_token_dir / "ui_token")
            assert status["project"] == identifier and status["domain"] == project.domain
            assert status["research_state"] == "PAUSED"
            assert status["initialized"] and status["awakening_ready"]
            assert status["autonomous_task_alive"] is False
            assert (project.runtime_dir / ".research-engine.lock").is_file()
            assert not (project.workspace_root / "data" / "tool_test.txt").exists()
            assert not any(project.workspace_root.iterdir())
            assert httpx.get(f"http://127.0.0.1:{port}/api/runtime/v1/status",
                             timeout=2, trust_env=False).status_code == 401
            if identifier == "crypto_lab":
                duplicate = subprocess.run(argv, cwd=repo, env=environment,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                           timeout=15, check=False)
                assert duplicate.returncode == 3
                assert duplicate.stderr.strip() == b"PROJECT_ENGINE_ALREADY_ACTIVE"
        # Both real engines coexist only because their Projects are disjoint.
        assert all(child.poll() is None for child in children.values())
        assert _LocalOllama.requests and set(_LocalOllama.requests) == {"/api/tags"}
    finally:
        for child in children.values():
            _stop_exact_child(child)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        async def cleanup() -> None:
            connection = await asyncpg.connect(dsn, timeout=5)
            try:
                for identifier in ("crypto_lab", "weather_lab"):
                    schema = projects[identifier].postgres_schema
                    await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await connection.close()
        asyncio.run(cleanup())
        # A stopped process cannot retain the kernel lease.
        from core.project_engine_lease import ProjectEngineLease
        for identifier in ("crypto_lab", "weather_lab"):
            with ProjectEngineLease(projects[identifier]):
                pass
