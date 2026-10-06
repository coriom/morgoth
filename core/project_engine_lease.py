"""Host-local, process-lifetime lease for one managed Project research engine."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat

from core.project import ProjectConfig


class ProjectEngineLeaseError(RuntimeError):
    """A managed Project cannot safely be leased for research."""


class ProjectEngineLease:
    """Hold one Linux advisory lock until the owning process closes it."""

    def __init__(self, project: ProjectConfig) -> None:
        """Bind a managed Project without opening storage yet."""
        if project.is_legacy:
            raise ProjectEngineLeaseError("PROJECT_ENGINE_LEASE_UNAVAILABLE")
        self._project_id = project.id
        self._runtime_dir = project.runtime_dir
        self._fd: int | None = None
        self._owner_pid: int | None = None

    def acquire(self) -> "ProjectEngineLease":
        """Acquire a nonblocking exclusive lease, failing closed on unsafe files."""
        if self._fd is not None:
            raise ProjectEngineLeaseError("PROJECT_ENGINE_ALREADY_ACTIVE")
        directory_fd: int | None = None
        file_fd: int | None = None
        try:
            directory = self._runtime_dir
            if any(part.is_symlink() for part in (directory, *directory.parents)):
                raise ProjectEngineLeaseError("PROJECT_ENGINE_LEASE_UNAVAILABLE")
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            directory_info = os.fstat(directory_fd)
            if (directory_info.st_uid != os.geteuid()
                    or directory_info.st_mode & 0o077):
                raise ProjectEngineLeaseError("PROJECT_ENGINE_LEASE_UNAVAILABLE")
            file_fd = os.open(
                ".research-engine.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ProjectEngineLeaseError("PROJECT_ENGINE_LEASE_UNAVAILABLE")
            try:
                fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProjectEngineLeaseError("PROJECT_ENGINE_ALREADY_ACTIVE") from None
            self._fd, file_fd = file_fd, None
            self._owner_pid = os.getpid()
            return self
        except OSError:
            raise ProjectEngineLeaseError("PROJECT_ENGINE_LEASE_UNAVAILABLE") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def release(self) -> None:
        """Release only this instance's descriptor; leave the harmless lock file."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self._owner_pid = None

    def held_for(self, project: ProjectConfig) -> bool:
        """Confirm this process still owns a descriptor for the selected Project."""
        return (self._fd is not None and self._owner_pid == os.getpid()
                and not project.is_legacy and self._project_id == project.id
                and self._runtime_dir == project.runtime_dir)

    def __enter__(self) -> "ProjectEngineLease":
        """Acquire the lease for a process-scoped context."""
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        """Release the lease when the research process leaves its context."""
        self.release()
