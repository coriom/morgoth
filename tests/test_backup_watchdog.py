"""Backup catch-up watchdog: detects stale backups, spawns fresh, never blocks."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core import backup_watchdog as bw


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("MORGOTH_BACKUP_MAX_AGE_HOURS", raising=False)


class TestKnobs:
    def test_default_max_age_is_24_hours(self):
        assert bw.backup_max_age_hours() == 24

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MORGOTH_BACKUP_MAX_AGE_HOURS", "6")
        assert bw.backup_max_age_hours() == 6

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("MORGOTH_BACKUP_MAX_AGE_HOURS", "nope")
        assert bw.backup_max_age_hours() == 24


class TestParseTsDir:
    def test_valid_timestamp_parses(self):
        got = bw._parse_ts_dir("20260921_142152")
        assert got is not None
        assert got.year == 2026 and got.month == 9 and got.day == 21
        assert got.hour == 14 and got.minute == 21

    def test_invalid_dirname_returns_none(self):
        assert bw._parse_ts_dir("not-a-timestamp") is None
        assert bw._parse_ts_dir("backup.log") is None
        assert bw._parse_ts_dir("2026-09-21") is None


class TestLatestBackupInfo:
    def test_empty_root_returns_none(self, tmp_path):
        assert bw.latest_backup_info(tmp_path) is None

    def test_picks_newest_by_timestamp(self, tmp_path):
        (tmp_path / "20260901_040001").mkdir()
        (tmp_path / "20260913_040001").mkdir()
        (tmp_path / "20260921_142152").mkdir()
        # A stray non-timestamp dir must be ignored.
        (tmp_path / "logs").mkdir()
        (tmp_path / "20260921_142152" / "postgres.sql.gz").write_bytes(b"x" * 1000)
        info = bw.latest_backup_info(tmp_path)
        assert info is not None
        assert info["path"].endswith("20260921_142152")
        assert info["size_bytes"] == 1000

    def test_age_is_non_negative_and_grows(self, tmp_path):
        (tmp_path / "20260101_040001").mkdir()
        info = bw.latest_backup_info(tmp_path)
        assert info is not None
        assert info["age_seconds"] > 0


class TestFormatters:
    @pytest.mark.parametrize("secs, expected_start", [
        (30, "30s"), (120, "2m"), (7200, "2.0h"),
        (86400, "1.0d"), (86400 * 2, "2.0d"),
    ])
    def test_format_age(self, secs, expected_start):
        assert bw.format_age(secs).startswith(expected_start[:3])

    @pytest.mark.parametrize("bytes_, expected", [
        (500, "500B"), (5 * 1024, "5.0K"),
        (5 * 1024**2, "5.0M"), (5 * 1024**3, "5.0G"),
    ])
    def test_format_size(self, bytes_, expected):
        assert bw.format_size(bytes_) == expected


@pytest.mark.asyncio
class TestCatchUpIfStale:
    async def test_skip_when_recent_backup_exists(self, monkeypatch, tmp_path):
        # Create a "recent" backup (parseable timestamp, age well under 24h).
        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        stamp = recent.strftime("%Y%m%d_%H%M%S")
        (tmp_path / stamp).mkdir()
        # Point the module at the tmp root + a real script.
        monkeypatch.setattr(bw, "BACKUP_ROOT", tmp_path)
        script = tmp_path / "backup.sh"; script.write_text("#!/bin/bash\ntrue\n")
        script.chmod(0o755)
        monkeypatch.setattr(bw, "BACKUP_SCRIPT", script)
        with patch.object(bw, "_spawn_backup_script",
                            AsyncMock(side_effect=AssertionError("must not spawn"))):
            res = await bw.catch_up_if_stale()
        assert res is not None and res["action"] == "skip"

    async def test_spawn_when_backup_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bw, "BACKUP_ROOT", tmp_path)  # empty
        script = tmp_path / "backup.sh"; script.write_text("#!/bin/bash\ntrue\n")
        script.chmod(0o755)
        monkeypatch.setattr(bw, "BACKUP_SCRIPT", script)
        fake = AsyncMock(return_value=AsyncMock(pid=12345))
        with patch.object(bw, "_spawn_backup_script", fake):
            res = await bw.catch_up_if_stale()
        assert res is not None and res["action"] == "spawned"
        assert res["pid"] == 12345

    async def test_spawn_when_stale(self, monkeypatch, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        stamp = old.strftime("%Y%m%d_%H%M%S")
        (tmp_path / stamp).mkdir()
        monkeypatch.setattr(bw, "BACKUP_ROOT", tmp_path)
        script = tmp_path / "backup.sh"; script.write_text("#!/bin/bash\ntrue\n")
        script.chmod(0o755)
        monkeypatch.setattr(bw, "BACKUP_SCRIPT", script)
        fake = AsyncMock(return_value=AsyncMock(pid=999))
        with patch.object(bw, "_spawn_backup_script", fake):
            res = await bw.catch_up_if_stale()
        assert res["action"] == "spawned"
        assert res["prior_age_seconds"] >= 48 * 3600 - 60

    async def test_missing_script_returns_none_non_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bw, "BACKUP_ROOT", tmp_path)
        monkeypatch.setattr(bw, "BACKUP_SCRIPT", tmp_path / "does_not_exist.sh")
        assert await bw.catch_up_if_stale() is None

    async def test_spawn_failure_is_non_fatal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bw, "BACKUP_ROOT", tmp_path)
        script = tmp_path / "backup.sh"; script.write_text("#!/bin/bash\ntrue\n")
        script.chmod(0o755)
        monkeypatch.setattr(bw, "BACKUP_SCRIPT", script)
        with patch.object(bw, "_spawn_backup_script",
                            AsyncMock(side_effect=OSError("no fork"))):
            res = await bw.catch_up_if_stale()
        assert res["action"] == "spawn_failed"


class TestSessionReportWiring:
    def test_report_renders_backup_line_when_populated(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.backup_age_seconds = 3600 * 4  # 4 h
        r.backup_size_bytes = 15 * 1024 * 1024  # 15 MB
        out = r.render()
        assert "BACKUP" in out
        assert "4.0h ago" in out
        assert "15.0M" in out
        # No warning at 4 h.
        assert "48h" not in out

    def test_report_warns_when_backup_older_than_48h(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.backup_age_seconds = 3600 * 72  # 3 d
        r.backup_size_bytes = 10 * 1024 * 1024
        out = r.render()
        assert "⚠ >48h" in out

    def test_report_flags_no_backup(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.backup_age_seconds = -1.0
        out = r.render()
        assert "none found" in out
        assert "WARNING" in out


class TestBrainWiring:
    def test_brain_calls_catch_up_at_startup(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "from core.backup_watchdog import catch_up_if_stale" in src
        assert "await _bcu()" in src
