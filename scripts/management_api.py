"""Explicit, loopback-only launcher for the offline Project management API."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

import uvicorn

from core.runtime import RuntimeHome
from api.management_app import ManagementSecurity, create_management_app
from project_manager import ProjectManager, ProjectManagerError


def read_private_token(path: Path) -> str:
    """Read only an explicitly supplied, private regular token file."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ValueError("management token file is unavailable or unsafe") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 256):
            raise ValueError("management token file must be private and regular")
        raw = os.read(fd, 257)
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").removesuffix("\n")
        ManagementSecurity(token=token, port=1)
    except (UnicodeError, ValueError):
        raise ValueError("management token file has invalid content") from None
    return token


def main(argv: list[str] | None = None) -> int:
    """Launch one local worker; no engine or legacy token is initialized."""
    parser = argparse.ArgumentParser(prog="python -m scripts.management_api")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.home.is_absolute():
            raise ValueError("application home must be absolute")
        token = read_private_token(args.token_file)
        security = ManagementSecurity(token=token, port=args.port)
        home = RuntimeHome(args.home, args.home / "projects")
        app = create_management_app(ProjectManager(home), security)
    except (ValueError, ProjectManagerError):
        print("management API configuration is invalid", file=sys.stderr)
        return 2
    # Never let ambient Uvicorn settings widen the explicit local launcher.
    for key in tuple(os.environ):
        if key.startswith("UVICORN_") or key == "FORWARDED_ALLOW_IPS":
            os.environ.pop(key, None)
    uvicorn.run(app, host="127.0.0.1", port=security.port, workers=1, reload=False,
                proxy_headers=False, forwarded_allow_ips="", access_log=False,
                server_header=False, date_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
