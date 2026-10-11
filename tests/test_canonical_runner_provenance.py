"""No-inference provenance and command-parity tests for the canonical runner."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from self_modify import canonical_runner as runner
from self_modify import gates


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(root: Path, identity: str) -> tuple[Path, str]:
    root.mkdir()
    _git(root, "init", "-q", "-b", "test")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "remote", "add", "origin", "git@github.com:coriom/morgoth.git")
    (root / ".gitignore").write_text(".env\ndata/\nvault/\n", encoding="utf-8")
    for name in runner._PROVENANCE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}: {identity}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root / "self_modify" / "canonical_runner.py", _git(root, "rev-parse", "HEAD")


def test_module_checkout_wins_over_cwd_and_historical_tree(tmp_path: Path,
                                                            monkeypatch) -> None:
    feature_module, feature_sha = _repo(tmp_path / "feature", "FEATURE_ONLY")
    _repo(tmp_path / "historical", "HISTORICAL_ONLY")
    monkeypatch.chdir(tmp_path / "historical")
    source, sha = runner.verified_source(feature_module)
    assert source == tmp_path / "feature" and sha == feature_sha
    copied = tmp_path / "sandbox" / "repo"
    digests = runner.copy_verified_source(source, copied, sha)
    assert "FEATURE_ONLY" in (copied / "tests/test_codex_critical_errors.py").read_text()
    assert "HISTORICAL_ONLY" not in (copied / "tests/test_codex_critical_errors.py").read_text()
    assert all(original == sandbox for original, sandbox in digests.values())
    assert len(digests["TREE_MANIFEST"][0]) == 64
    assert not (copied / ".git").exists()


def test_exclusions_and_dirty_source_fail_closed(tmp_path: Path) -> None:
    module, sha = _repo(tmp_path / "feature", "SAFE")
    source = module.parent.parent
    (source / ".env").write_text("SYNTHETIC_SECRET\n")
    (source / "data").mkdir()
    (source / "data" / "secret.txt").write_text("SYNTHETIC_SECRET\n")
    (source / "vault").mkdir()
    (source / "vault" / "secret.txt").write_text("SYNTHETIC_SECRET\n")
    dirty = source / "dirty.txt"
    dirty.write_text("uncommitted\n")
    with pytest.raises(runner.SourceProvenanceError, match="source_worktree_dirty"):
        runner.verified_source(module)
    dirty.unlink()
    destination = tmp_path / "sandbox"
    runner.copy_verified_source(source, destination, sha)
    for name in (".env", ".git", "data", "vault"):
        assert not (destination / name).exists()


def test_main_copies_its_module_tree_and_uses_exact_gate_builder(tmp_path: Path,
                                                                  monkeypatch, capsys) -> None:
    module, sha = _repo(tmp_path / "feature", "FEATURE_ONLY")
    _repo(tmp_path / "historical", "HISTORICAL_ONLY")
    monkeypatch.setattr(runner, "__file__", str(module))
    monkeypatch.chdir(tmp_path / "historical")
    monkeypatch.setattr(sys, "argv", ["canonical_runner", "--maxfail=1"])
    monkeypatch.setattr(gates, "sandbox_posture", lambda: {"ok": True, "reason": ""})
    seen: list[str] = []
    actual_copy = runner.copy_verified_source
    def spy_copy(source, destination, expected_sha):
        result = actual_copy(source, destination, expected_sha)
        assert source == tmp_path / "feature"
        assert "FEATURE_ONLY" in (destination / "tests/test_codex_critical_errors.py").read_text()
        seen.append(str(destination))
        return result
    monkeypatch.setattr(runner, "copy_verified_source", spy_copy)
    def spy_command(argv):
        serialized = " ".join(argv)
        for required in ("systemd-run", "--property=MemoryMax=", "unshare",
                         "--net", "bwrap", "--clearenv", "--tmpfs", "--share-net",
                         "--dist=loadfile", "--max-worker-restart=3",
                         "--disable-socket", "--allow-unix-socket", "--timeout=60",
                         "not integration", "--maxfail=1"):
            assert required in serialized
        assert len(seen) == 1
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(runner, "_run_canonical_command", spy_command)
    assert runner.main() == 0
    output = capsys.readouterr().out
    assert f"REQUESTED_SOURCE_SHA={sha}" in output
    assert f"TESTED_SOURCE_SHA={sha}" in output
    assert "SOURCE_SHA256 tests/test_codex_critical_errors.py" in output
    assert "SANDBOX_SHA256 tests/test_codex_critical_errors.py" in output
    assert not Path(seen[0]).exists()


def test_wrong_origin_and_copy_mutation_are_rejected(tmp_path: Path,
                                                     monkeypatch) -> None:
    module, sha = _repo(tmp_path / "feature", "SAFE")
    source = module.parent.parent
    _git(source, "remote", "set-url", "origin", "git@github.com:other/repo.git")
    with pytest.raises(runner.SourceProvenanceError, match="source_checkout_unexpected"):
        runner.verified_source(module)
    real_copy = runner.shutil.copytree
    def mutated_copy(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        if Path(src) == source:
            (Path(dst) / "tests/test_apply.py").write_text("MUTATED\n")
        return result
    monkeypatch.setattr(runner.shutil, "copytree", mutated_copy)
    with pytest.raises(runner.SourceProvenanceError, match="source_copy_mismatch"):
        runner.copy_verified_source(source, tmp_path / "sandbox", sha)


def test_junit_scoreboard_distinguishes_results_from_missing_report(tmp_path: Path) -> None:
    report = tmp_path / "canonical.junit.xml"
    assert "collected=UNKNOWN" in runner._junit_scoreboard(report)
    report.write_text('<testsuites><testsuite tests="5" failures="1" errors="1" '
                      'skipped="1" /></testsuites>', encoding="utf-8")
    assert runner._junit_scoreboard(report) == (
        "collected=5 passed=2 failed=1 skipped=1 errors=1"
    )


def test_included_symlink_cannot_copy_outside_secret(tmp_path: Path) -> None:
    module, _ = _repo(tmp_path / "feature", "SAFE")
    source = module.parent.parent
    outside = tmp_path / "outside-secret"
    outside.write_text("TOP_SECRET_DO_NOT_COPY", encoding="utf-8")
    (source / "extra_link").symlink_to(outside)
    _git(source, "add", "extra_link")
    _git(source, "commit", "-q", "-m", "fixture link")
    sha = _git(source, "rev-parse", "HEAD")
    destination = tmp_path / "sandbox"
    with pytest.raises(runner.SourceProvenanceError, match="source_symlink_rejected"):
        runner.copy_verified_source(source, destination, sha)
    assert not destination.exists()


def test_full_manifest_rejects_mutated_unselected_file(tmp_path: Path,
                                                      monkeypatch) -> None:
    module, _ = _repo(tmp_path / "feature", "SAFE")
    source = module.parent.parent
    extra = source / "tests" / "other.py"
    extra.write_text("EXPECTED\n", encoding="utf-8")
    _git(source, "add", "tests/other.py")
    _git(source, "commit", "-q", "-m", "fixture extra file")
    sha = _git(source, "rev-parse", "HEAD")
    real_copy = runner.shutil.copytree
    def mutated_copy(src, dst, *args, **kwargs):
        result = real_copy(src, dst, *args, **kwargs)
        if Path(src) == source:
            (Path(dst) / "tests" / "other.py").write_text("MUTATED\n")
        return result
    monkeypatch.setattr(runner.shutil, "copytree", mutated_copy)
    with pytest.raises(runner.SourceProvenanceError, match="source_copy_mismatch"):
        runner.copy_verified_source(source, tmp_path / "sandbox", sha)


def test_timeout_reaps_owned_process_group_only(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "owned-child-pid"
    script = (
        "import pathlib,subprocess,time,sys; "
        "p=subprocess.Popen(['sleep','60']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    unrelated = subprocess.Popen(["sleep", "60"])
    monkeypatch.setattr(gates, "PYTEST_BUDGET_SECS", 1)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            runner._run_canonical_command([sys.executable, "-c", script, str(marker)])
        assert marker.is_file()
        owned_pid = int(marker.read_text())
        for _ in range(20):
            stat_path = Path(f"/proc/{owned_pid}/stat")
            if not stat_path.exists() or stat_path.read_text().split()[2] == "Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail("owned descendant remained running after timeout")
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
