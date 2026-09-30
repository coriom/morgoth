"""Project-scoped LLM profiles: the service boundary shared by CLI and desktop.

No import-time I/O, credentials in state, or mutable process-wide selection.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import fcntl
import json
import os
import stat
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import TYPE_CHECKING, AsyncIterator, Mapping

from core.llm import tasks
from core.project import Project

if TYPE_CHECKING:
    from core.llm.environment import Environment


class ProfileError(ValueError):
    """Safe operator error; never includes configuration values or DB errors."""


@dataclass(frozen=True)
class LLMProfile:
    """Immutable routing policy; empty routes explicitly delegate to legacy."""
    id: str
    routes: Mapping[str, str]

    def __post_init__(self) -> None:
        from core.llm.registry import _parse_spec
        if self.routes and set(self.routes) != set(tasks.all_tasks()):
            raise ProfileError("managed profile must cover every task")
        for spec in self.routes.values():
            _parse_spec(spec)
        object.__setattr__(self, 'routes', MappingProxyType(dict(self.routes)))


PROFILES = MappingProxyType({
    identifier: LLMProfile(identifier, routes)
    for identifier, routes in (
        ('legacy', {}),
        ('claude', {task: 'ollama:default' if task == tasks.CHAT else 'claude-cli:default' for task in tasks.all_tasks()}),
        ('codex', {task: 'ollama:default' if task == tasks.CHAT else 'codex-cli:default' for task in tasks.all_tasks()}),
    )
})


def list_profiles(project: Project) -> tuple[LLMProfile, ...]:
    """Return available immutable policies (catalog is currently built-in)."""
    return tuple(PROFILES.values())


def state_path(project: Project) -> Path:
    """Use the canonical Project runtime root without changing its semantics."""
    return project.runtime_dir / 'llm-profile.json'


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate profile state key')
        result[key] = value
    return result


def current_profile(project: Project) -> LLMProfile:
    """Read one atomic snapshot; absent state is the exact legacy boundary."""
    path = state_path(project)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return PROFILES['legacy']
    except OSError:
        raise ProfileError('cannot read project LLM profile state') from None
    try:
        with os.fdopen(fd) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('profile state must be a regular file')
            raw = stream.read(4097)
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if len(raw) > 4096 or not isinstance(data, dict) or set(data) != {'schema_version', 'profile'}:
            raise ValueError
        if type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise ValueError
        return PROFILES[data['profile']]
    except (ValueError, KeyError, TypeError, OSError):
        raise ProfileError('malformed or unknown project LLM profile state') from None


def resolve_task_route(project: Project, task: str, *, profile: LLMProfile | None = None) -> tuple[str, str]:
    """Resolve at workload time; managed routes shadow all legacy task overrides."""
    from core.llm.registry import _parse_spec, resolve_legacy
    if task not in tasks.DEFAULTS:
        raise KeyError('unknown LLM task')
    selected = profile if profile is not None else current_profile(project)
    spec = selected.routes.get(task)
    return _parse_spec(spec) if spec else resolve_legacy(task, project)


def shadowed_overrides(project: Project) -> tuple[str, ...]:
    """Report override NAMES only, never arbitrary environment values."""
    selected = current_profile(project)
    names = []
    for task in selected.routes:
        for name in (f'MORGOTH_LLM_{task.upper()}', *tasks.LEGACY_ALIASES.get(task, ()),
                     *({'reflect': ('REFLECT_PROVIDER',), 'shadow': ('SHADOW_PROVIDER',)}.get(task, ()))):
            if os.environ.get(name):
                names.append(name)
        if task in project.llm_overrides:
            names.append(f'Project.llm_overrides.{task}')
    return tuple(names)


def provider_status(provider: str, environment: Environment | None) -> tuple[str, str]:
    """Readiness is separate from presence; qualification takes precedence."""
    from core.llm.codex_cli import SAFE_FOR_WORKLOADS
    if provider == 'codex-cli' and not SAFE_FOR_WORKLOADS:
        return 'BLOCKED', 'Codex workload qualification has not passed (NOT_QUALIFIED); binary presence is insufficient'
    attribute = {'ollama': 'ollama', 'claude-cli': 'claude_cli', 'codex-cli': 'codex_cli', 'api': 'api_key'}.get(provider)
    cap = getattr(environment, attribute, None) if attribute else None
    if cap is not None and cap.status == 'ok':
        return 'READY', 'non-inference preconditions pass; authentication/quota not tested'
    return 'UNAVAILABLE', 'provider preconditions unavailable'


def profile_status(project: Project, profile: LLMProfile, environment: Environment) -> tuple[str, str]:
    """Aggregate effective provider readiness without performing inference."""
    statuses = [provider_status(resolve_task_route(project, task, profile=profile)[0], environment)
                for task in tasks.all_tasks()]
    return next((s for s in statuses if s[0] == 'BLOCKED'),
                next((s for s in statuses if s[0] != 'READY'), ('READY', 'all provider preconditions pass')))


def recommended_profile(project: Project, environment: Environment) -> str | None:
    """Recommend only ready managed profiles; registry order is preference order."""
    return next((p.id for p in list_profiles(project) if p.routes and profile_status(project, p, environment)[0] == 'READY'), None)


@asynccontextmanager
async def campaign_guard(project: Project) -> AsyncIterator[None]:
    """Serialize with campaign INSERT/UPDATE until profile publication completes.

    No initialization, expiry processing or row writes. A short SHARE table lock
    blocks concurrent campaign creation; project schema never falls back to public.
    """
    import asyncpg
    from core.campaign_lifecycle import CAMPAIGN_LIVE_SQL
    from core.storage_namespace import validate_identifier
    dsn = os.environ.get('POSTGRES_URL')
    if not dsn:
        raise ProfileError('campaign guard unavailable: POSTGRES_URL required')
    conn = None
    try:
        conn = await asyncpg.connect(dsn, timeout=5, server_settings={
            'search_path': validate_identifier(project.postgres_schema),
            'statement_timeout': '5000', 'lock_timeout': '3000',
        })
        async with conn.transaction():
            await conn.execute('LOCK TABLE campaigns IN SHARE MODE')
            if await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM campaigns WHERE {CAMPAIGN_LIVE_SQL})'):
                raise ProfileError('profile change refused: Project has a research-live campaign')
            yield
    except ProfileError:
        raise
    except Exception:
        raise ProfileError('campaign guard unavailable; profile change refused') from None
    finally:
        if conn is not None:
            await conn.close()


def _publish(project: Project, identifier: str) -> None:
    path = state_path(project)
    fd, name = tempfile.mkstemp(prefix='.llm-profile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'schema_version': 1, 'profile': identifier}, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


async def switch_profile(project: Project, target: str) -> LLMProfile:
    """One guarded atomic action; no force option or environment activation path."""
    from core.llm.environment import detect_environment
    try:
        selected = PROFILES[target]
    except KeyError:
        raise ProfileError('unknown LLM profile') from None
    current = current_profile(project)
    from core.llm.registry import _parse_spec
    for spec in selected.routes.values():
        status, reason = provider_status(_parse_spec(spec)[0], None)
        if status == 'BLOCKED':
            raise ProfileError(f'{status}: {reason}')
    if current.id == target:
        return selected
    if selected.routes:
        status, reason = profile_status(project, selected, await detect_environment())
        if status != 'READY':
            raise ProfileError(f'{status}: {reason}')
    project.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(project.runtime_dir / '.llm-profile.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if current_profile(project).id == target:
                return selected
            async with campaign_guard(project):
                # Small bounded file write; never await between publication and
                # guard release, including cancellation while worker threads run.
                _publish(project, target)
        return selected
    except OSError:
        raise ProfileError('profile state unavailable or switch already in progress') from None
