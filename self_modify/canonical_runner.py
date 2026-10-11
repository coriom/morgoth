"""Canonical hermetic tests from the verified checkout containing this module.

The copied source must be the clean Git HEAD of this module's own worktree.
Pytest argv and all confinement layers come from the reviewed gate builder.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from self_modify import gates


_EXPECTED_ORIGINS = frozenset({"git@github.com:coriom/morgoth.git",
                               "https://github.com/coriom/morgoth.git"})
_PROVENANCE_FILES = ("self_modify/canonical_runner.py", "self_modify/gates.py",
                     "tests/test_apply.py", "tests/test_codex_critical_errors.py")


class SourceProvenanceError(RuntimeError):
    """The selected test source cannot be identified as a clean checkout."""


def _git(root: Path, *args: str) -> str:
    """Read bounded Git metadata without exposing command stderr."""
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True, timeout=10,
                                check=True)
    except (OSError, subprocess.SubprocessError):
        raise SourceProvenanceError("source_git_unavailable") from None
    return result.stdout.strip()


def verified_source(module_file: Path) -> tuple[Path, str]:
    """Resolve the module's own clean coriom/morgoth checkout and HEAD."""
    try:
        module = module_file.resolve(strict=True)
        root = module.parent.parent
        if module != root / "self_modify" / "canonical_runner.py":
            raise SourceProvenanceError("source_module_location_invalid")
        top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != root or _git(root, "remote", "get-url", "origin") not in _EXPECTED_ORIGINS:
            raise SourceProvenanceError("source_checkout_unexpected")
        sha = _git(root, "rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SourceProvenanceError("source_head_invalid")
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            raise SourceProvenanceError("source_worktree_dirty")
        return root, sha
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, SourceProvenanceError):
            raise
        raise SourceProvenanceError("source_unavailable") from None


def copy_verified_source(source: Path, destination: Path, expected_sha: str,
                         files: tuple[str, ...] = _PROVENANCE_FILES) -> dict[str, tuple[str, str]]:
    """Copy excluded source without symlinks and verify every copied file."""
    if _git(source, "rev-parse", "HEAD") != expected_sha:
        raise SourceProvenanceError("source_head_changed")
    before = _tree_manifest(source)
    # Preserve links instead of following them if a source entry changes
    # between the preflight walk and copy; the post-copy walk then rejects it.
    shutil.copytree(source, destination, ignore=gates._SANDBOX_IGNORE,
                    symlinks=True)
    after = _tree_manifest(destination)
    if before != after:
        raise SourceProvenanceError("source_copy_mismatch")
    digests: dict[str, tuple[str, str]] = {}
    for name in files:
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise SourceProvenanceError("source_path_invalid")
        original, copied = source / rel, destination / rel
        if not original.is_file() or not copied.is_file():
            raise SourceProvenanceError("source_copy_incomplete")
        source_digest = hashlib.sha256(original.read_bytes()).hexdigest()
        copied_digest = hashlib.sha256(copied.read_bytes()).hexdigest()
        if source_digest != copied_digest:
            raise SourceProvenanceError("source_copy_mismatch")
        digests[name] = (source_digest, copied_digest)
    tree_digest = hashlib.sha256()
    for name, digest in sorted(before.items()):
        tree_digest.update(name.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
    digests["TREE_MANIFEST"] = (tree_digest.hexdigest(), tree_digest.hexdigest())
    if (_git(source, "rev-parse", "HEAD") != expected_sha
            or _git(source, "status", "--porcelain", "--untracked-files=all")):
        raise SourceProvenanceError("source_changed_during_copy")
    return digests


def _tree_manifest(root: Path) -> dict[str, str]:
    """Hash included regular files; fail closed on any included symlink/type."""
    manifest: dict[str, str] = {}

    def visit(directory: Path) -> None:
        entries = list(os.scandir(directory))
        ignored = gates._SANDBOX_IGNORE(str(directory), [entry.name for entry in entries])
        for entry in entries:
            if entry.name in ignored:
                continue
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise SourceProvenanceError("source_symlink_rejected")
            path = Path(entry.path)
            if stat.S_ISDIR(mode):
                visit(path)
            elif stat.S_ISREG(mode):
                manifest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                raise SourceProvenanceError("source_type_rejected")

    visit(root)
    return manifest


def _run_canonical_command(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute the owned gate process and report only its numerical scope data."""
    child = subprocess.Popen(argv, env=gates._hardened_outer_env(),
                             start_new_session=True)
    deadline = time.monotonic() + gates.PYTEST_BUDGET_SECS
    scope: Path | None = None
    peak_bytes = 0
    max_pids = 0
    oom_kill = 0
    pids_limit_hits = 0
    try:
        while child.poll() is None:
            scope = scope or _owned_scope(child.pid)
            if scope is not None:
                peak_bytes = max(peak_bytes, _numeric_file(scope / "memory.peak"))
                max_pids = max(max_pids, _numeric_file(scope / "pids.current"))
                oom_kill = max(oom_kill, _event_count(scope / "memory.events", "oom_kill"))
                pids_limit_hits = max(pids_limit_hits,
                                      _event_count(scope / "pids.events", "max"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, gates.PYTEST_BUDGET_SECS)
            try:
                child.wait(timeout=min(remaining, 0.5))
            except subprocess.TimeoutExpired:
                pass
        if scope is not None:
            peak_bytes = max(peak_bytes, _numeric_file(scope / "memory.peak"))
            max_pids = max(max_pids, _numeric_file(scope / "pids.current"))
            oom_kill = max(oom_kill, _event_count(scope / "memory.events", "oom_kill"))
            pids_limit_hits = max(pids_limit_hits,
                                  _event_count(scope / "pids.events", "max"))
        return subprocess.CompletedProcess(argv, child.returncode)
    except BaseException:
        _terminate_owned_group(child)
        raise
    finally:
        print(f"OWNED_SCOPE_MEMORY_PEAK_BYTES={peak_bytes if scope else 'UNKNOWN'} "
              f"OWNED_SCOPE_MAX_PIDS={max_pids if scope else 'UNKNOWN'} "
              f"OWNED_SCOPE_OOM_KILLS={oom_kill if scope else 'UNKNOWN'} "
              f"OWNED_SCOPE_PIDS_LIMIT_HITS={pids_limit_hits if scope else 'UNKNOWN'}",
              flush=True)


def _owned_scope(pid: int) -> Path | None:
    """Locate only the systemd scope containing our exact launcher PID."""
    try:
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        for row in rows:
            hierarchy, _, relative = row.split(":", 2)
            if hierarchy == "0" and relative.endswith(".scope"):
                result = Path("/sys/fs/cgroup") / relative.lstrip("/")
                if result.name.startswith("run-") and result.is_dir():
                    return result
    except (OSError, ValueError):
        return None
    return None


def _numeric_file(path: Path) -> int:
    try:
        return max(0, int(path.read_text().strip()))
    except (OSError, ValueError):
        return 0


def _event_count(path: Path, key: str) -> int:
    try:
        for row in path.read_text().splitlines():
            name, value = row.split()
            if name == key:
                return max(0, int(value))
    except (OSError, ValueError):
        return 0
    return 0


def _terminate_owned_group(child: subprocess.Popen[bytes]) -> None:
    """Kill and reap only the launcher's dedicated process group."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def _junit_scoreboard(path: Path) -> str:
    """Return bounded JUnit totals; an absent report is not a test pass."""
    try:
        root = ET.parse(path).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is None:
            raise ValueError("missing suite")
        collected = int(suite.attrib["tests"])
        failed = int(suite.attrib.get("failures", "0"))
        errors = int(suite.attrib.get("errors", "0"))
        skipped = int(suite.attrib.get("skipped", "0"))
        passed = collected - failed - errors - skipped
        if min(collected, passed, failed, errors, skipped) < 0:
            raise ValueError("invalid totals")
        return (f"collected={collected} passed={passed} failed={failed} "
                f"skipped={skipped} errors={errors}")
    except (OSError, ET.ParseError, KeyError, ValueError):
        return "collected=UNKNOWN passed=UNKNOWN failed=UNKNOWN skipped=UNKNOWN errors=UNKNOWN"


def main() -> int:
    """Run the copied checkout under the gate's exact hermetic command."""
    try:
        source, requested_sha = verified_source(Path(__file__))
    except SourceProvenanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    posture = gates.sandbox_posture()
    if not posture["ok"]:
        print(f"ERROR: sandbox unavailable ({posture['reason']})", file=sys.stderr)
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="morgoth_test_"))
    try:
        copied = sandbox / "repo"
        try:
            digests = copy_verified_source(source, copied, requested_sha)
        except SourceProvenanceError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"REQUESTED_SOURCE_SHA={requested_sha}", flush=True)
        print(f"TESTED_SOURCE_SHA={requested_sha}", flush=True)
        for name, (original, copied_digest) in sorted(digests.items()):
            print(f"SOURCE_SHA256 {name} {original}", flush=True)
            print(f"SANDBOX_SHA256 {name} {copied_digest}", flush=True)
        argv = gates._build_pytest_argv(
            copied, isolated=True, confined=True, cgroup_bound=True,
            junit_out=copied / "canonical.junit.xml",
            extra_pytest_args=sys.argv[1:],
        )
        t0 = time.monotonic()
        try:
            completed = _run_canonical_command(argv)
        except subprocess.TimeoutExpired:
            print(_junit_scoreboard(copied / "canonical.junit.xml"), flush=True)
            print("ERROR: canonical_pytest_timeout", file=sys.stderr)
            return 124
        print(_junit_scoreboard(copied / "canonical.junit.xml"), flush=True)
        print(f"canonical wall={time.monotonic() - t0:.1f}s exit={completed.returncode}")
        return completed.returncode
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
