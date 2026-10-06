"""Pinned-project, loopback-only, paused desktop research-engine launcher.

No engine or token-binding module is imported until Project selection and the
explicit native-parent environment have been validated.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


REQUIRED_ENV = (
    "POSTGRES_URL", "OLLAMA_BASE_URL", "OLLAMA_PRIMARY_MODEL",
    "OLLAMA_AGENT_MODEL", "SECRET_KEY", "MAX_CONCURRENT_AGENTS",
    "LOG_RETENTION_DAYS", "LOG_LEVEL_THOUGHT",
)


def configure(home: Path, project_id: str, port: int) -> None:
    """Select a managed Project before importing the engine or token module."""
    if not home.is_absolute() or not home.is_dir() or not 1 <= port <= 65535:
        raise ValueError("invalid home or port")
    if not project_id or project_id == "default":
        raise ValueError("desktop research requires an existing managed Project")
    missing = tuple(name for name in REQUIRED_ENV if not os.environ.get(name))
    if missing:
        raise ValueError("missing runtime variables: " + ", ".join(missing))
    os.environ["MORGOTH_HOME"] = str(home)
    os.environ["MORGOTH_PROJECT"] = project_id
    os.environ.pop("MORGOTH_DOMAIN", None)
    for name in tuple(os.environ):
        if name.startswith("UVICORN_") or name == "FORWARDED_ALLOW_IPS":
            os.environ.pop(name, None)
    from core.project import current_project

    project = current_project()
    if project.is_legacy or project.id != project_id:
        raise ValueError("desktop research requires an existing managed Project")


def main(argv: list[str] | None = None) -> int:
    """Run one selected Project on loopback with research initially paused."""
    parser = argparse.ArgumentParser(prog="python -m scripts.research_engine")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        configure(args.home, args.project, args.port)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("missing runtime variables:"):
            print(message, file=sys.stderr)
        else:
            print("desktop research configuration is invalid", file=sys.stderr)
        return 2
    except Exception:
        print("desktop research Project is unavailable", file=sys.stderr)
        return 2

    from core.project import current_project
    from core.project_engine_lease import ProjectEngineLease, ProjectEngineLeaseError

    try:
        # This must precede api.server/api.token imports and all runtime startup.
        with ProjectEngineLease(current_project()) as lease:
            import uvicorn
            from api.server import app
            from api.routes.runtime import _code_sha

            app.state.project_engine_lease = lease
            app.state.desktop_runtime = True
            app.state.desktop_expected_port = args.port
            app.state.code_sha = _code_sha()
            uvicorn.run(
                app, host="127.0.0.1", port=args.port, workers=1, reload=False,
                proxy_headers=False, forwarded_allow_ips="", access_log=False,
                server_header=False, date_header=False,
            )
    except ProjectEngineLeaseError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
