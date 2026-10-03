"""Offline Project catalog CLI; also usable with python -m scripts.projects_cli."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from core.runtime import runtime_home
from project_manager import ProjectManager, ProjectManagerError


_EXIT = {
    "INVALID_REQUEST": 2, "UNKNOWN_DOMAIN": 2, "UNKNOWN_PROJECT": 2,
    "ALREADY_EXISTS": 3, "INVALID_CATALOG": 4, "NAMESPACE_CONFLICT": 4,
    "PATH_CONFLICT": 4, "PUBLICATION_FAILED": 5,
}


def _parser() -> argparse.ArgumentParser:
    """Build one thin command parser; management rules live in the service."""
    parser = argparse.ArgumentParser(prog="morgoth projects")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "show", "create", "validate"):
        command = commands.add_parser(name)
        if name in ("show", "create", "validate"):
            command.add_argument("project_id")
        if name == "create":
            command.add_argument("--name", required=True)
            command.add_argument("--domain", required=True)
        command.add_argument("--json", action="store_true")
    return parser


def _human(result: dict[str, Any], command: str) -> str:
    """Render only safe configuration fields, never operational readiness."""
    if command == "list":
        lines = [f"{item['id']}  {item['name']}  domain={item['domain']}"
                 for item in result["projects"]]
        return "\n".join(lines + ["configuration valid; runtime unchecked"])
    project = result["project"] if command in ("create", "validate") else result
    lines = [f"{project['id']}  {project['name']}  domain={project['domain']}",
             f"manifest: {project['manifest_path'] or '(built-in legacy)'}",
             f"workspace: {project['workspace_root'] or '(legacy default)'}",
             f"PostgreSQL schema: {project['postgres_schema']}",
             f"Chroma prefix: {project['chroma_prefix']!r}",
             f"vault: {project['vault_dir']}", f"runtime: {project['runtime_dir']}",
             "configuration valid; runtime unchecked"]
    if command == "create":
        lines.append("created locally; engine not started; storage not initialized")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Dispatch offline management with stable structured errors."""
    args = _parser().parse_args(argv)
    try:
        manager = ProjectManager(runtime_home())
        if args.command == "list":
            result = manager.list_projects()
        elif args.command == "show":
            result = manager.get_project(args.project_id)
        elif args.command == "validate":
            result = manager.validate_project(args.project_id)
        else:
            result = manager.create_project(
                args.project_id, name=args.name, domain=args.domain)
    except ProjectManagerError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, sort_keys=True),
              file=sys.stderr)
        return _EXIT.get(exc.code, 5)
    except ValueError:
        print(json.dumps({"error": {"code": "INVALID_REQUEST",
                                    "message": "invalid Project home configuration"}},
                         sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True) if args.json else _human(result, args.command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
