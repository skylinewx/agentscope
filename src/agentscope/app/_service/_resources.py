# -*- coding: utf-8 -*-
"""User resource policy, workspace bindings and leased MCP runtimes."""

# pylint: disable=protected-access

import asyncio
import hashlib
import inspect
import json
import os
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field

from ..._logging import logger
from ..._utils._common import _describe_exception
from ...mcp import MCPClient
from ...skill import Skill
from ...tool import ToolBase, Toolkit
from ...workspace import LocalWorkspace, WorkspaceBase
from ...workspace._local_workspace import _hash_skill_directory
from ..resources import LocalSkillStore
from ..resources._environment import resolve_environment, _ENV_REF
from ..storage import MCPRecord, SkillRecord, StorageBase, SkillResourceBinding
from ._resource_config_service import ResourceConfigService
from ._resource_resolution import EffectiveResource

_run_toolkits: ContextVar[list | None] = ContextVar(
    "resource_toolkits", default=None
)


def _serialize_bindings(operation: Any) -> Any:
    """Protect the workspace sidecar, independently of database switches."""

    @wraps(operation)
    async def serialized(
        self: Any,
        user_id: str,
        workspace: WorkspaceBase,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        async with workspace._resource_lock:
            return await operation(self, user_id, workspace, *args, **kwargs)

    return serialized


@asynccontextmanager
async def resource_toolkit_scope() -> Any:
    """Release every assembled toolkit, including early/error return paths."""
    toolkits: list = []
    token = _run_toolkits.set(toolkits)
    try:
        yield
    finally:
        _run_toolkits.reset(token)
        for toolkit in toolkits:
            await asyncio.shield(toolkit.aclose())


class SkillBinding(BaseModel):
    """Installation metadata only. Availability lives in database config."""

    resource_id: str
    installed_hash: str


class WorkspaceBindings(BaseModel):
    """Session installation index, never consulted to select resources."""

    version: int = 2
    skills: dict[str, dict[str, SkillBinding]] = Field(default_factory=dict)
    scopes: dict[str, tuple[str, str]] = Field(default_factory=dict)


@dataclass(eq=False)
class _MCPEntry:
    """A connection owned by one task and borrowed by runs and tool calls."""

    client: MCPClient
    user_id: str
    resource_id: str
    fingerprint: str
    workspace_generation: tuple[int, int] | None = None
    tools: list[ToolBase] = field(default_factory=list)
    users: int = 0
    retired: bool = False
    last_used: float = field(default_factory=time.monotonic)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    owner: asyncio.Task | None = None

    async def start(self, timeout: float = 30.0) -> None:
        """Enter and exit MCP/AnyIO transport contexts in the same task."""
        ready = asyncio.get_running_loop().create_future()

        async def own_connection() -> None:
            try:
                await self.client.connect()
                self.tools = await self.client.list_tools()
                ready.set_result(None)
                await self.stop.wait()
            except BaseException as exc:
                if not ready.done():
                    if isinstance(exc, asyncio.CancelledError):
                        if self.stop.is_set():
                            ready.cancel()
                        else:
                            ready.set_exception(
                                RuntimeError(
                                    f"MCP '{self.client.name}' initialization "
                                    "was cancelled."
                                )
                            )
                    else:
                        ready.set_exception(exc)
                else:
                    logger.warning(
                        "MCP connection owner stopped: %s", type(exc).__name__
                    )
            finally:
                if self.client.is_connected:
                    await self.client.close()

        self.owner = asyncio.create_task(own_connection())
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(ready)
        except BaseException:
            self.stop.set()
            if not ready.done():
                self.owner.cancel()
            await asyncio.shield(self.owner)
            if ready.done() and not ready.cancelled():
                ready.exception()
            raise

    async def release(self) -> None:
        """Drain a retired connection only after its final borrower exits."""
        self.users -= 1
        self.last_used = time.monotonic()
        if self.retired and self.users == 0:
            self.stop.set()
            if self.owner is not None:
                await asyncio.shield(self.owner)

    async def retire(self) -> None:
        """Stop accepting new snapshots; current snapshots remain valid."""
        self.retired = True
        if self.users == 0:
            self.stop.set()
            if self.owner is not None:
                await asyncio.shield(self.owner)


class _LeasedTool(ToolBase):
    """Keep a background MCP call alive after its chat toolkit is released."""

    def __init__(self, tool: ToolBase, entry: _MCPEntry) -> None:
        super().__init__()
        self._tool, self._entry = tool, entry
        for name in (
            "name",
            "description",
            "input_schema",
            "is_concurrency_safe",
            "is_read_only",
            "is_external_tool",
            "is_state_injected",
            "is_mcp",
            "mcp_name",
            "metadata_schema",
        ):
            setattr(self, name, getattr(tool, name))

    async def check_permissions(self, *args: Any, **kwargs: Any) -> Any:
        return await self._tool.check_permissions(*args, **kwargs)

    async def call(self, **kwargs: Any) -> Any:
        self._entry.users += 1
        try:
            result = self._tool(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if inspect.isasyncgen(result):
                try:
                    async for chunk in result:
                        yield chunk
                finally:
                    await result.aclose()
            else:
                yield result
        finally:
            await self._entry.release()


class ResourceToolkit(Toolkit):
    """A toolkit whose MCP snapshot is explicitly released at run end."""

    def __init__(self, entries: list[_MCPEntry], **kwargs: Any) -> None:
        self._resource_entries = entries
        super().__init__(**kwargs)
        tracked = _run_toolkits.get()
        if tracked is not None:
            tracked.append(self)

    async def aclose(self) -> None:
        """Idempotently release run leases; background calls keep theirs."""
        entries, self._resource_entries = self._resource_entries, []
        for entry in entries:
            await entry.release()


async def close_resource_toolkit(toolkit: Toolkit | None) -> None:
    """Leave ordinary caller-supplied toolkits alone."""
    if isinstance(toolkit, ResourceToolkit):
        await toolkit.aclose()


class ResourceService(ResourceConfigService):
    """Resolve agent defaults and session overrides for each new run.

    Listings never open MCP connections. Files and leased clients survive
    disabling a resource, while new runs only receive effective selections.
    """

    def __init__(
        self,
        storage: StorageBase,
        skill_store: LocalSkillStore | None = None,
        max_idle_clients: int = 40,
        access=None,
    ) -> None:
        super().__init__(storage, access)
        self.skill_store = skill_store or LocalSkillStore(
            "./resource-artifacts"
        )
        self._pool: dict[tuple[str, str, str, str, str], _MCPEntry] = {}
        self._entries: set[_MCPEntry] = set()
        self._health: dict[tuple[str, str, str, str, str], dict] = {}
        self._pool_lock = asyncio.Lock()
        self._max_idle_clients = max_idle_clients

    @staticmethod
    def source(record: MCPRecord | SkillRecord) -> str:
        """Describe the record's origin without exposing its configuration."""
        return "hub" if record.hub_id else "custom"

    @staticmethod
    def _bindings_path(workspace: WorkspaceBase, user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode()).hexdigest()[:24]
        return workspace.get_backend().join_path(
            workspace.workdir, f".resource-installs-{digest}.json"
        )

    async def _load_bindings(
        self, workspace: WorkspaceBase, user_id: str
    ) -> WorkspaceBindings:
        backend = workspace.get_backend()
        path = self._bindings_path(workspace, user_id)
        if not await backend.file_exists(path):
            return WorkspaceBindings()
        state = WorkspaceBindings.model_validate_json(
            await backend.read_file(path)
        )
        if state.version != 2:
            raise ValueError("Unsupported workspace resource binding version.")
        return state

    async def _save_bindings(
        self, workspace: WorkspaceBase, user_id: str, state: WorkspaceBindings
    ) -> None:
        path = self._bindings_path(workspace, user_id)
        data = state.model_dump_json(indent=2).encode()
        if isinstance(workspace, LocalWorkspace):

            def publish() -> None:
                temporary = f"{path}.{uuid.uuid4().hex}.tmp"
                try:
                    Path(temporary).write_bytes(data)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)

            await asyncio.to_thread(publish)
        else:
            await workspace.get_backend().write_file(path, data)

    async def equip_mcp(
        self, user_id, workspace, agent_id, session_id, record
    ):
        return await self.add_session_resource(
            user_id, agent_id, session_id, "mcp", record
        )

    async def remove_mcp(
        self, user_id, workspace, agent_id, session_id, resource_id
    ):
        await self.change_session_resource(
            user_id, agent_id, session_id, "mcp", resource_id, remove=True
        )
        await self.invalidate(
            user_id, resource_id, workspace.workspace_id, agent_id, session_id
        )

    async def change_session_resource(
        self,
        user_id,
        agent_id,
        session_id,
        kind,
        resource_id,
        enabled=None,
        *,
        remove=False,
    ):
        """Publish a scope change and drain only its cached MCP generation."""
        result = await super().change_session_resource(
            user_id,
            agent_id,
            session_id,
            kind,
            resource_id,
            enabled,
            remove=remove,
        )
        if kind == "mcp":
            await self.invalidate(
                user_id, resource_id, agent_id=agent_id, session_id=session_id
            )
        return result

    async def list_mcps(self, user_id, workspace, agent_id, session_id):
        results = []
        for selection, record in await self.effective(
            user_id, agent_id, session_id
        ):
            if selection.kind != "mcp":
                continue
            key = (
                user_id,
                workspace.workspace_id,
                agent_id,
                session_id,
                selection.resource_id,
            )
            health = self._health.get(
                key, {"status": "not_loaded", "tools": [], "error": None}
            )
            active = bool(record and selection.enabled)
            results.append(
                {
                    **selection.scope_fields(),
                    "resource_id": selection.resource_id,
                    "name": record.name if record else selection.resource_id,
                    "is_stateful": (
                        record.client.is_stateful if record else False
                    ),
                    "transport": (
                        record.client.mcp_config.type if record else "unknown"
                    ),
                    "available": record is not None,
                    "effective_enabled": active,
                    "source": self.source(record) if record else "custom",
                    "version": record.version if record else None,
                    "status": (
                        health["status"]
                        if active
                        else "disabled" if record else "unavailable"
                    ),
                    "is_healthy": active and health["status"] == "ready",
                    "tools": health["tools"] if active else [],
                    "error": health["error"] if active else None,
                }
            )
        return results

    async def _skill_hash(
        self, workspace: WorkspaceBase, directory: str
    ) -> str:
        if isinstance(workspace, LocalWorkspace):
            return await asyncio.to_thread(_hash_skill_directory, directory)
        backend = workspace.get_backend()
        digest = hashlib.sha256(b"agentscope-skill-tree-v2\0")

        async def visit(path: str, prefix: str) -> None:
            for name in sorted(await backend.list_dir(path)):
                child = backend.join_path(path, name)
                relative = f"{prefix}/{name}" if prefix else name
                encoded = relative.encode("utf-8", "surrogateescape")
                is_dir = await backend.is_dir(child)
                digest.update(b"D" if is_dir else b"F")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                if is_dir:
                    await visit(child, relative)
                else:
                    digest.update(
                        hashlib.sha256(await backend.read_file(child)).digest()
                    )

        await visit(directory, "")
        return digest.hexdigest()

    @staticmethod
    def _skill_scope(user_id, agent_id, session_id, resource_id, binding_id):
        payload = json.dumps(
            [user_id, agent_id, session_id, resource_id, binding_id]
        )
        return "resource_" + hashlib.sha256(payload.encode()).hexdigest()

    @_serialize_bindings
    async def _session_skill(
        self,
        user_id,
        workspace,
        agent_id,
        session_id,
        selection,
        *,
        install=False,
    ):
        binding = selection.binding
        if (
            not isinstance(binding, SkillResourceBinding)
            or not binding.artifact_id
        ):
            raise ValueError("Skill binding has no saved package.")
        scope = self._skill_scope(
            user_id,
            agent_id,
            session_id,
            selection.resource_id,
            binding.binding_id,
        )
        state = await self._load_bindings(workspace, user_id)
        installed = state.skills.setdefault(scope, {})
        for skill in await workspace.list_skills(agent_id=scope):
            metadata = installed.get(skill.dir)
            if metadata and metadata.installed_hash == binding.artifact_id:
                return skill
        if not install:
            return None
        await workspace.add_skill(
            self.skill_store.path(binding.artifact_id), agent_id=scope
        )
        for skill in await workspace.list_skills(agent_id=scope):
            if (
                await self._skill_hash(workspace, skill.dir)
                == binding.artifact_id
            ):
                installed[skill.dir] = SkillBinding(
                    resource_id=selection.resource_id,
                    installed_hash=binding.artifact_id,
                )
                state.scopes[scope] = (agent_id, session_id)
                await self._save_bindings(workspace, user_id, state)
                return skill
        raise ValueError("Skill install did not produce the saved package.")

    async def register_skill_package(self, user_id, package):
        record = await self.storage.get_skill_by_name(user_id, package.name)
        if record is not None:
            if record.artifact_id != package.artifact_id:
                raise ValueError(
                    "A Skill with this name has different contents. "
                    "Update its saved package first."
                )
            return record
        record = SkillRecord(
            user_id=user_id,
            name=package.name,
            description=package.description,
            markdown=package.markdown,
        )
        record.update_version(package.artifact_id, package.artifact_id[:12])
        await self.storage.upsert_skill(user_id, record)
        return record

    async def equip_skill(
        self,
        user_id,
        workspace,
        agent_id,
        session_id,
        record,
        *,
        upgrade=False,
    ):
        """Stage a copy before publishing a binding or version change."""
        _, session = await self.configs(user_id, agent_id, session_id)
        effective = await self.effective(user_id, agent_id, session_id)
        previous = next(
            (
                item
                for item, _ in effective
                if item.kind == "skill" and item.resource_id == record.id
            ),
            None,
        )
        local = session.config.resource_config.skills.get(record.id)
        if local and not upgrade:
            await self._session_skill(
                user_id,
                workspace,
                agent_id,
                session_id,
                previous,
                install=True,
            )
            return False
        if record.user_id != user_id:
            raise ValueError("Skill is not available in your library.")
        if not record.artifact_id:
            raise ValueError("Skill has no saved package.")
        try:
            self.skill_store.path(record.artifact_id)
        except (ValueError, FileNotFoundError) as exc:
            raise ValueError("Saved Skill package is unavailable.") from exc
        if upgrade and previous:
            skill = await self._session_skill(
                user_id, workspace, agent_id, session_id, previous
            )
            if (
                skill
                and await self._skill_hash(workspace, skill.dir)
                != previous.binding.artifact_id
            ):
                raise ValueError(
                    "This session Skill was modified. "
                    "Preserve or remove your copy before upgrading."
                )
        binding = (
            local.model_copy(deep=True) if local else SkillResourceBinding()
        )
        binding.artifact_id = record.artifact_id
        binding.version = record.version
        binding.version_updated_at = record.version_updated_at
        if upgrade and previous:
            binding.enabled = previous.enabled
        selection = EffectiveResource(
            kind="skill",
            resource_id=record.id,
            binding=binding,
            binding_sources=["session"],
            agent_enabled=None,
            session_override=None,
            enabled=binding.enabled,
            local=True,
        )
        await self._session_skill(
            user_id, workspace, agent_id, session_id, selection, install=True
        )
        if upgrade:

            def mutate(latest):
                current = latest.config.resource_config
                if current.skills.get(record.id) != local:
                    from fastapi import HTTPException

                    raise HTTPException(
                        409, "Skill binding changed. Reload before upgrading."
                    )
                current.skills[record.id] = binding
                current.skill_overrides.pop(record.id, None)

            await self.storage.mutate_session(
                user_id, agent_id, session_id, mutate
            )
        else:
            await self.add_session_resource(
                user_id,
                agent_id,
                session_id,
                "skill",
                record,
                prepared=binding,
            )
        return True

    async def list_skills(self, user_id, workspace, agent_id, session_id):
        results = []
        for selection, record in await self.effective(
            user_id, agent_id, session_id
        ):
            if selection.kind != "skill":
                continue
            skill, error = None, None
            if record:
                try:
                    skill = await self._session_skill(
                        user_id, workspace, agent_id, session_id, selection
                    )
                    if skill is None:
                        self.skill_store.path(selection.binding.artifact_id)
                except (ValueError, FileNotFoundError):
                    error = "Saved Skill package is unavailable."
            available = record is not None and error is None
            results.append(
                {
                    **selection.scope_fields(),
                    "resource_id": selection.resource_id,
                    "name": record.name if record else selection.resource_id,
                    "description": record.description if record else "",
                    "dir": skill.dir if skill else "",
                    "markdown": skill.markdown if skill else "",
                    "updated_at": skill.updated_at if skill else None,
                    "available": available,
                    "effective_enabled": available and selection.enabled,
                    "source": self.source(record) if record else "custom",
                    "version": record.version if record else None,
                    "version_updated_at": (
                        record.version_updated_at if record else None
                    ),
                    "installed_version": selection.binding.version,
                    "installed_version_updated_at": (
                        selection.binding.version_updated_at
                    ),
                    "status": (
                        "unavailable"
                        if not available
                        else (
                            "disabled"
                            if not selection.enabled
                            else "ready" if skill else "not_loaded"
                        )
                    ),
                    "error": error,
                }
            )
        return results

    @staticmethod
    def _safe_error(exc: Exception, record: MCPRecord) -> str:
        message = _describe_exception(exc)
        # Do not echo stored secrets or resolved environment references.
        secrets: list[str] = []

        def collect(value: Any, direct: bool = False) -> None:
            if isinstance(value, str):
                if direct and value:
                    secrets.append(value)
                secrets.extend(
                    os.environ[match.group(1)]
                    for match in _ENV_REF.finditer(value)
                    if os.environ.get(match.group(1))
                )
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item, direct)
            elif isinstance(value, list):
                for item in value:
                    collect(item, direct)

        collect(record.values, direct=True)
        config = record.client.mcp_config.model_dump()
        collect(config)
        for key in ("headers", "env"):
            collect(config.get(key), direct=True)
        collect(config.get("args"), direct=True)
        for key, value in (config.get("headers") or {}).items():
            if key.lower() == "authorization" and isinstance(value, str):
                secrets.append(value.rsplit(" ", 1)[-1])
        if config.get("url"):
            secrets.append(config["url"])
            try:
                url = urlsplit(config["url"])
                secrets.extend(value for _, value in parse_qsl(url.query))
                if url.password:
                    secrets.append(url.password)
            except ValueError:
                pass
        for value in sorted(set(secrets), key=len, reverse=True):
            if isinstance(value, str):
                try:
                    resolved = resolve_environment(value)
                    if resolved:
                        message = message.replace(resolved, "[redacted]")
                except ValueError:
                    pass
        return message

    async def snapshot(
        self,
        user_id: str,
        workspace: WorkspaceBase,
        agent_id: str,
        session_id: str,
    ) -> tuple[list[ToolBase], list[Skill], list[_MCPEntry]]:
        """Acquire an availability snapshot for a chat or live connection."""
        effective = await self.effective(user_id, agent_id, session_id)
        names = Counter(
            (item.kind, record.name)
            for item, record in effective
            if record and item.enabled
        )
        available_skills = []
        for selection, record in effective:
            if (
                selection.kind != "skill"
                or not selection.enabled
                or record is None
            ):
                continue
            try:
                skill = await self._session_skill(
                    user_id,
                    workspace,
                    agent_id,
                    session_id,
                    selection,
                    install=True,
                )
                name = record.name
                if names[("skill", name)] > 1:
                    name = f"{name} ({selection.resource_id[:10]})"
                available_skills.append(replace(skill, name=name))
            except (ValueError, FileNotFoundError, OSError) as exc:
                logger.warning(
                    "Skill %s unavailable: %s",
                    selection.resource_id,
                    type(exc).__name__,
                )
        tools, entries = [], []
        generation = (id(workspace), id(getattr(workspace, "_gateway", None)))
        try:
            async with self._pool_lock:
                active_ids = {
                    item.resource_id
                    for item, record in effective
                    if item.kind == "mcp" and item.enabled and record
                }
                for key in list(self._pool):
                    if (
                        key[:4]
                        == (
                            user_id,
                            workspace.workspace_id,
                            agent_id,
                            session_id,
                        )
                        and key[4] not in active_ids
                    ):
                        await self._pool.pop(key).retire()
                        self._health.pop(key, None)
                for selection, record in effective:
                    if selection.kind != "mcp":
                        continue
                    resource_id = selection.resource_id
                    key = (
                        user_id,
                        workspace.workspace_id,
                        agent_id,
                        session_id,
                        resource_id,
                    )
                    if not record or not selection.enabled:
                        entry = self._pool.pop(key, None)
                        if entry:
                            await entry.retire()
                        continue
                    try:
                        data = resolve_environment(
                            record.client.model_dump(mode="json")
                        )
                        if names[("mcp", record.name)] > 1:
                            data["name"] = f"{record.name}_{resource_id[:10]}"
                        fingerprint = hashlib.sha256(
                            json.dumps(data, sort_keys=True).encode()
                        ).hexdigest()
                        entry = self._pool.get(key)
                        if entry and (
                            entry.fingerprint != fingerprint
                            or entry.workspace_generation != generation
                            or (entry.owner is not None and entry.owner.done())
                        ):
                            self._pool.pop(key)
                            await entry.retire()
                            entry = None
                        if entry is None:
                            resolved = MCPClient.model_validate(data)
                            client = workspace.create_mcp_client(
                                resolved,
                                agent_id=agent_id,
                                session_id=session_id,
                                instance_id=uuid.uuid4().hex,
                            )
                            entry = _MCPEntry(
                                client,
                                user_id,
                                resource_id,
                                fingerprint,
                                workspace_generation=generation,
                            )
                            await entry.start()
                            self._pool[key] = entry
                            self._entries.add(entry)

                            def discard_owner(
                                _task: asyncio.Task,
                                item: _MCPEntry = entry,
                            ) -> None:
                                self._entries.discard(item)

                            entry.owner.add_done_callback(discard_owner)
                        entry.users += 1
                        entries.append(entry)
                        tools.extend(
                            _LeasedTool(tool, entry) for tool in entry.tools
                        )
                        self._health[key] = {
                            "status": "ready",
                            "error": None,
                            "tools": [
                                {
                                    "name": tool.name,
                                    "description": tool.description,
                                }
                                for tool in entry.tools
                            ],
                        }
                    except Exception as exc:
                        self._health[key] = {
                            "status": "error",
                            "error": self._safe_error(exc, record),
                            "tools": [],
                        }
                idle = sorted(
                    (
                        item
                        for item in self._pool.items()
                        if item[1].users == 0
                    ),
                    key=lambda item: item[1].last_used,
                )
                for key, entry in idle[
                    : max(0, len(idle) - self._max_idle_clients)
                ]:
                    self._pool.pop(key)
                    await entry.retire()
                    self._health.pop(key, None)
            return tools, available_skills, entries
        except BaseException:
            for entry in entries:
                await entry.release()
            raise

    async def invalidate(
        self,
        user_id: str,
        resource_id: str,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Retire selected generations after a switch/config/library edit."""
        async with self._pool_lock:
            for key in list(self._pool):
                if key[0] != user_id or key[4] != resource_id:
                    continue
                if workspace_id is not None and key[1] != workspace_id:
                    continue
                if agent_id is not None and key[2] != agent_id:
                    continue
                if session_id is not None and key[3] != session_id:
                    continue
                await self._pool.pop(key).retire()
                self._health.pop(key, None)

    @_serialize_bindings
    async def purge_session(self, user_id, workspace, agent_id, session_id):
        """Drop this session's installed copies and retire its MCP clients."""
        state = await self._load_bindings(workspace, user_id)
        for scope, context in list(state.scopes.items()):
            if context == (agent_id, session_id):
                await workspace.purge_agent(agent_id=scope)
                state.skills.pop(scope, None)
                state.scopes.pop(scope, None)
        await self._save_bindings(workspace, user_id, state)
        async with self._pool_lock:
            for key in list(self._pool):
                if key[0] == user_id and key[2:4] == (agent_id, session_id):
                    await self._pool.pop(key).retire()
                    self._health.pop(key, None)

    async def close(self) -> None:
        """Close connections after chat and background runtimes stop."""
        for entry in self._entries:
            entry.retired = True
            entry.stop.set()
        owners = [e.owner for e in self._entries if e.owner is not None]
        if owners:
            await asyncio.gather(*owners, return_exceptions=True)
        self._pool.clear()
        self._entries.clear()
