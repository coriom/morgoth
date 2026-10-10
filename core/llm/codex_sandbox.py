"""Mandatory Linux Bubblewrap envelope for the Codex CLI transport.

Only a private invocation directory is writable on the host. The operator's
Codex auth file is mounted read-only; it is readable by the confined process.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat


class SandboxUnavailable(RuntimeError):
    """A fixed, non-sensitive sandbox prerequisite failed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _regular(path: Path, *, executable: bool = False) -> Path:
    """Resolve a trusted native executable without accepting a missing target."""
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError:
        raise SandboxUnavailable("sandbox_runtime_missing") from None
    if not stat.S_ISREG(mode) or (executable and not os.access(resolved, os.X_OK)):
        raise SandboxUnavailable("sandbox_runtime_invalid")
    return resolved


def build_sandbox_argv(binary: str, cwd: str, env: dict[str, str],
                       cli_args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Build the only permitted Codex process command and cleared host env."""
    if os.name != "posix" or not Path("/proc/self/ns/mnt").exists():
        raise SandboxUnavailable("sandbox_linux_required")
    bwrap = _regular(Path("/usr/bin/bwrap"), executable=True)
    codex = _regular(Path(binary), executable=True)
    home_name = env.get("CODEX_HOME") or str(Path(env.get("HOME", "")) / ".codex")
    home = Path(home_name)
    if not home.is_absolute() or ".." in home.parts:
        raise SandboxUnavailable("sandbox_auth_path_invalid")
    for ancestor in (home, *home.parents):
        if ancestor.is_symlink():
            raise SandboxUnavailable("sandbox_auth_unsafe")
    auth = home / "auth.json"
    try:
        parent = home.lstat()
        info = auth.lstat()
    except OSError:
        raise SandboxUnavailable("sandbox_auth_missing") from None
    if (not stat.S_ISDIR(parent.st_mode) or not stat.S_ISREG(info.st_mode)
            or parent.st_uid != os.getuid() or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or parent.st_mode & 0o022
            or info.st_nlink != 1):
        raise SandboxUnavailable("sandbox_auth_unsafe")
    if codex.name == "codex.js" and codex.parent.name == "bin":
        package = codex.parent.parent
        if not (package / "package.json").is_file():
            raise SandboxUnavailable("sandbox_codex_layout_invalid")
        native = package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
        _regular(native, executable=True)
        node_name = shutil.which("node", path=env.get("PATH", ""))
        if node_name is None:
            raise SandboxUnavailable("sandbox_node_missing")
        node = _regular(Path(node_name), executable=True)
        runtime_mounts = ["--ro-bind", str(package), "/opt/codex",
                          "--dir", "/opt/node", "--ro-bind", str(node), "/opt/node/node"]
        runtime_path = "/opt/node:/usr/bin:/bin"
        confined_binary = "/opt/codex/bin/codex.js"
    elif (codex.name == "codex" and codex.parent.name == "bin"
          and codex.parent.parent.name == "0.162.1-x86_64-unknown-linux-musl"
          and codex.parent.parent.parent.name == "releases"
          and codex.parent.parent.parent.parent.name == "standalone"
          and codex.parent.parent.parent.parent.parent.name == "packages"
          and codex.parent.parent.parent.parent.parent.parent.name == ".codex"
          and codex.parent.parent.parent.parent.parent.parent.stat().st_uid == os.getuid()):
        # Exact reviewed standalone release. Mount the ELF itself, never
        # the containing installation, auth or config tree.
        runtime_mounts = ["--dir", "/opt/codex", "--dir", "/opt/codex/bin",
                          "--ro-bind", str(codex), "/opt/codex/bin/codex"]
        runtime_path = "/usr/bin:/bin"
        confined_binary = "/opt/codex/bin/codex"
    else:
        raise SandboxUnavailable("sandbox_codex_layout_invalid")
    workspace = Path(cwd)
    try:
        workspace_info = workspace.lstat()
    except OSError:
        raise SandboxUnavailable("sandbox_workspace_invalid") from None
    if (not workspace.is_absolute() or workspace.parent != Path("/tmp")
            or not workspace.name.startswith(("morgoth-codex-", "morgoth-codex-check-"))
            or not stat.S_ISDIR(workspace_info.st_mode)
            or workspace_info.st_uid != os.getuid() or workspace_info.st_mode & 0o077):
        raise SandboxUnavailable("sandbox_workspace_invalid")
    mounts = ["/usr", "/lib", "/lib64", "/etc/resolv.conf", "/etc/hosts",
              "/etc/nsswitch.conf", "/etc/ssl"]
    if any(not Path(path).exists() for path in mounts):
        raise SandboxUnavailable("sandbox_system_mount_missing")
    command = [str(bwrap), "--unshare-all", "--unshare-user", "--share-net", "--die-with-parent",
               "--disable-userns", "--ro-bind", "/usr", "/usr",
               "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
               "--dir", "/etc"]
    for path in mounts[3:]:
        command += ["--ro-bind", path, path]
    command += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--dir", str(workspace), "--bind", str(workspace), str(workspace),
                "--dir", "/opt",
                *runtime_mounts,
                "--dir", "/home", "--dir", "/home/codex",
                "--dir", "/home/codex/.codex",
                "--ro-bind", str(auth), "/home/codex/.codex/auth.json",
                "--chdir", str(workspace), "--clearenv",
                "--setenv", "HOME", "/home/codex",
                "--setenv", "CODEX_HOME", "/home/codex/.codex",
                "--setenv", "PATH", runtime_path,
                "--setenv", "LANG", "C.UTF-8",
                "--", confined_binary, *cli_args]
    return command, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
