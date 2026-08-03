"""Local UI session token.

Any local process can hit http://localhost:8000 (CORS allows every
localhost origin — see server._LOCAL_ORIGIN_REGEX), so a bare mutation
endpoint like POST /api/proposals/{id}/approve would be callable by
ANY web page the operator happens to have open. That's not acceptable
for the self-modification kill-switch.

Design: on first access, generate a 32-byte URL-safe token and write it
to ``~/.morgoth/ui_token`` with mode 0600 (parent dir 0700). The Next.js
server reads the file server-side and injects it into ``X-Morgoth-Token``
on proxied mutations. The browser JS never sees the token.

The file lives in the operator's home dir, not in the repo — so it's
not accidentally committed, and it survives service restarts (regenerating
would log the browser out of the cockpit on every restart).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


TOKEN_DIR = Path.home() / ".morgoth"
TOKEN_PATH = TOKEN_DIR / "ui_token"
HEADER_NAME = "X-Morgoth-Token"


def ensure_ui_token(path: Path = TOKEN_PATH) -> str:
    """Return the UI session token, generating it on first call.

    Parent dir 0700, file 0600. If the file already exists, it is read
    unchanged (a fresh restart must not invalidate the cockpit session).
    """
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    # Write with restrictive umask to avoid a window where the file
    # exists group/world-readable.
    prev_umask = os.umask(0o077)
    try:
        path.write_text(token)
        os.chmod(path, 0o600)
    finally:
        os.umask(prev_umask)
    return token


def read_ui_token(path: Path = TOKEN_PATH) -> str | None:
    """Read the token WITHOUT creating it. Returns None if absent."""
    if not path.exists():
        return None
    return path.read_text().strip() or None
