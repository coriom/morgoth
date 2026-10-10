"""Canonical hermetic tests from the verified checkout containing this module.

The copied source must be the clean Git HEAD of this module's own worktree.
Pytest argv and all confinement layers come from the reviewed gate builder.
"""

from __future__ import annotations

import hashlib
import re
import shutil
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
    """Copy excluded source and verify representative bytes and HEAD stability."""
    if _git(source, "rev-parse", "HEAD") != expected_sha:
        raise SourceProvenanceError("source_head_changed")
    shutil.copytree(source, destination, ignore=gates._SANDBOX_IGNORE)
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
    if (_git(source, "rev-parse", "HEAD") != expected_sha
            or _git(source, "status", "--porcelain", "--untracked-files=all")):
        raise SourceProvenanceError("source_changed_during_copy")
    return digests


def _run_canonical_command(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute one bounded exact gate command with a scrubbed environment."""
    return subprocess.run(argv, env=gates._hardened_outer_env(),
                          timeout=gates.PYTEST_BUDGET_SECS,
                          start_new_session=True)


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
