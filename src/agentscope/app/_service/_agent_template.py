"""Isolated template installation and explicit, three-way user updates."""

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Collection
from uuid import UUID, uuid5
from urllib.parse import urlsplit

from fastapi import HTTPException
from ...permission import PermissionMode
from ..._logging import logger

from ..access import ResourceAccessPolicyBase, ResourceKind, ResourceRef
from ..auth import AuthService
from ..storage import (
    AgentData,
    AgentRecord,
    MCPRecord,
    SkillRecord,
    StorageBase,
)
from ..storage._model._agent_settings import AgentDefaults, AgentMemoryConfig
from ._agent_files import idle_workspace
from ._agent_management import revision
from ._mcp_edit import editable_mcp
from ._resource_config_service import ResourceConfigService

NAMESPACE = UUID("ac8a16d9-82c3-43cd-acb8-4e643bcbfb67")
FIELDS = (
    "name",
    "description",
    "system_prompt",
    "chat_config",
    "defaults",
    "memory_config",
    "resource_config",
)
MISSING = object()


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def identifier(*parts: str) -> str:
    return uuid5(NAMESPACE, json.dumps(parts)).hex


def credential_ids(data: AgentData) -> set[str]:
    defaults = data.defaults or AgentDefaults()
    configs = [
        getattr(defaults, key)
        for key in (
            "chat_model_config",
            "fallback_chat_model_config",
            "tts_model_config",
            "realtime_model_config",
        )
    ]
    if data.memory_config:
        configs.append(data.memory_config.retrieval_model_config)
    return {c.credential_id for c in configs if c}


def knowledge_ids(data: AgentData) -> set[str]:
    config = data.defaults.knowledge_config if data.defaults else None
    return set(config.knowledge_base_ids if config else [])


def agent_document(data: AgentData) -> dict[str, Any]:
    payload = data.model_dump(mode="json")
    payload["defaults"] = (data.defaults or AgentDefaults()).model_dump(
        mode="json"
    )
    payload["memory_config"] = (
        data.memory_config or AgentMemoryConfig()
    ).model_dump(mode="json")
    return {key: payload[key] for key in FIELDS}


def resource_document(
    kind: str, record: MCPRecord | SkillRecord
) -> dict[str, Any]:
    if kind == "mcps":
        return {
            "client": record.client.model_dump(mode="json"),
            "display_name": record.display_name,
            "description": record.description,
        }
    return {
        key: record.model_dump(mode="json")[key]
        for key in (
            "name",
            "display_name",
            "description",
            "artifact_id",
            "version",
            "version_updated_at",
            "markdown",
        )
    }


def merge_documents(
    base: Any,
    local: Any,
    incoming: Any,
    choices: Collection[str] = (),
    path: str = "",
    changes: list[dict[str, Any]] | None = None,
) -> Any:
    """Merge source changes only; explicit paths opt into conflicting updates."""
    if changes is None:
        changes = []
    if incoming == base:
        return copy.deepcopy(local) if local is not MISSING else MISSING
    if all(isinstance(v, dict) for v in (base, local, incoming)):
        result = {}
        for key in sorted(set(base) | set(local) | set(incoming)):
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            value = merge_documents(
                base.get(key, MISSING),
                local.get(key, MISSING),
                incoming.get(key, MISSING),
                choices,
                pointer,
                changes,
            )
            if value is not MISSING:
                result[key] = value
        return result
    conflict = local != base and local != incoming
    if local != incoming:
        changes.append(
            {
                "path": path,
                "conflict": conflict,
                "current": None if local is MISSING else local,
                "incoming": None if incoming is MISSING else incoming,
                "removed": incoming is MISSING,
            }
        )
    chosen = local if conflict and path not in choices else incoming
    return copy.deepcopy(chosen) if chosen is not MISSING else MISSING


def visible_document(document: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Keep saved MCP secrets write-only, including removed-resource previews."""
    visible = copy.deepcopy(document)
    for rid, payload in visible["mcps"].items():
        payload["client"] = editable_mcp(
            MCPRecord(id=rid, user_id=user_id, **payload)
        )
    return visible


def pointer_value(document: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:]:
        if not isinstance(document, dict):
            return None
        document = document.get(part.replace("~1", "/").replace("~0", "~"))
    return document


class TemplateAccessPolicy(ResourceAccessPolicyBase):
    """Share only the configured template's model/KB references to SSO users."""

    def __init__(
        self, auth: AuthService, delegate: ResourceAccessPolicyBase
    ) -> None:
        self.auth, self.delegate = auth, delegate

    async def list_accessible(
        self, viewer_id: str, kind: ResourceKind, storage: StorageBase
    ) -> list[ResourceRef]:
        refs = list(
            await self.delegate.list_accessible(viewer_id, kind, storage)
        )
        settings = self.auth.nvwa_settings
        if (
            kind not in {ResourceKind.CREDENTIAL, ResourceKind.KNOWLEDGE_BASE}
            or not settings.template_agent_id
        ):
            return refs
        user = await self.auth.store.user(viewer_id)
        if (
            not user
            or user["status"] != "active"
            or user.get("sso_provider") != "nvwa"
        ):
            return refs
        source = await storage.get_agent(
            settings.template_owner_id, settings.template_agent_id
        )
        if source is None or viewer_id == source.user_id:
            return refs
        ids = (
            credential_ids(source.data)
            if kind == ResourceKind.CREDENTIAL
            else knowledge_ids(source.data)
        )
        refs.extend(
            ResourceRef(kind=kind, owner_id=source.user_id, resource_id=rid)
            for rid in ids
        )
        return refs

    async def can_edit(
        self,
        viewer_id: str,
        kind: ResourceKind,
        owner_id: str,
        resource_id: str,
        storage: StorageBase,
    ) -> bool:
        # Template use grants never escalate an existing policy's edit rights.
        return await self.delegate.can_edit(
            viewer_id, kind, owner_id, resource_id, storage
        )


class AgentTemplateService:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.storage = state.storage
        self.auth = state.auth_service
        self.settings = self.auth.nvwa_settings if self.auth else None

    async def source(self) -> AgentRecord:
        if not self.settings or not self.settings.template_agent_id:
            raise HTTPException(
                409,
                "请配置 nvwa.template_owner_id 和 template_agent_id，指定来源智能体。",
            )
        record = await self.storage.get_agent(
            self.settings.template_owner_id, self.settings.template_agent_id
        )
        if record is None or record.source != "user":
            raise HTTPException(
                409, "来源智能体不存在，请联系管理员检查配置。"
            )
        if (
            not record.data.defaults
            or not record.data.defaults.chat_model_config
        ):
            raise HTTPException(409, "来源智能体尚未配置默认模型。")
        return record

    async def authorized(self, user_id: str) -> None:
        user = await self.auth.store.user(user_id)
        if (
            not user
            or user["status"] != "active"
            or user.get("sso_provider") != "nvwa"
        ):
            raise HTTPException(403, "此用户尚未通过女娲认证。")

    async def snapshot(
        self, user_id: str
    ) -> tuple[AgentRecord, dict[str, Any], dict[str, str]]:
        await self.authorized(user_id)
        source = await self.source()
        aid = identifier(user_id, source.user_id, source.id)
        data = source.data.model_copy(deep=True)
        data.defaults.permission_mode = PermissionMode.DEFAULT
        snapshot = {"agent": agent_document(data), "skills": {}, "mcps": {}}
        mcp_sources = {}
        for kind, singular in (("skills", "skill"), ("mcps", "mcp")):
            bindings = {}
            for rid, source_binding in getattr(
                data.resource_config, kind
            ).items():
                record = await getattr(self.storage, "get_" + singular)(
                    source.user_id, rid
                )
                if record is None:
                    raise HTTPException(409, f"来源 {singular} 资源已失效。")
                target_id = identifier(aid, kind, rid)
                binding = source_binding.model_copy(deep=True)
                binding.binding_id = identifier(
                    aid, kind, rid, source_binding.binding_id
                )
                if kind == "skills":
                    record = record.model_copy(deep=True)
                    record.artifact_id = source_binding.artifact_id
                    record.version = source_binding.version
                    record.version_updated_at = (
                        source_binding.version_updated_at
                    )
                    if not record.artifact_id:
                        raise HTTPException(
                            409, "来源 Skill 未固定安装包版本。"
                        )
                    try:
                        record.markdown = await self.state.resource_service.skill_store.read_file(
                            record.artifact_id, "SKILL.md"
                        )
                    except (ValueError, OSError, TypeError):
                        raise HTTPException(
                            409, "来源 Skill 安装包不可用。"
                        ) from None
                    binding.artifact_id = record.artifact_id
                    payload = resource_document(kind, record)
                else:
                    client = record.client.model_dump(mode="json")
                    config = client["mcp_config"]
                    if config.get("type") != "http_mcp":
                        raise HTTPException(
                            409,
                            "SSO 模板仅支持 HTTP MCP，请单独为用户配置本地 MCP。",
                        )
                    parsed = urlsplit(config["url"])
                    if parsed.path.endswith(("/sse", "/messages/")):
                        raise HTTPException(
                            409,
                            "SSO 模板需要 Streamable HTTP MCP，不支持旧 SSE。",
                        )
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.netloc
                        or parsed.username is not None
                        or parsed.query
                        or parsed.fragment
                        or "${" in config["url"]
                    ):
                        raise HTTPException(
                            409,
                            "模板 MCP 地址不能包含凭据、查询参数或环境变量。",
                        )
                    # Header values, hub answers and admin tokens are never cloned.
                    config["headers"] = None
                    payload = {
                        "client": client,
                        "display_name": record.display_name or record.name,
                        "description": record.description,
                    }
                    mcp_sources[target_id] = rid
                snapshot[kind][target_id] = payload
                bindings[target_id] = binding.model_dump(mode="json")
            snapshot["agent"]["resource_config"][kind] = bindings
        return source, snapshot, mcp_sources

    async def current(self, record: AgentRecord) -> dict[str, Any]:
        meta = record.data.template_sync or {}
        result = {
            "agent": agent_document(record.data),
            "skills": {},
            "mcps": {},
        }
        for kind, singular in (("skills", "skill"), ("mcps", "mcp")):
            for rid in meta.get("baseline", {}).get(kind, {}):
                resource = await getattr(self.storage, "get_" + singular)(
                    record.user_id, rid
                )
                if resource:
                    result[kind][rid] = resource_document(kind, resource)
        return result

    def check_source(self, record: AgentRecord) -> dict[str, Any]:
        meta = record.data.template_sync or {}
        if (
            not self.settings
            or meta.get("source_owner_id") != self.settings.template_owner_id
            or meta.get("source_agent_id") != self.settings.template_agent_id
        ):
            raise HTTPException(409, "此智能体没有可用的同步来源。")
        return meta

    async def preview(self, record: AgentRecord) -> dict[str, Any]:
        meta = self.check_source(record)
        source, incoming, _ = await self.snapshot(record.user_id)
        local = await self.current(record)
        changes = []
        merge_documents(meta["baseline"], local, incoming, changes=changes)
        visible = visible_document(local, record.user_id)
        for change in changes:
            change["current"] = pointer_value(visible, change["path"])
        return {
            "source_name": source.data.name,
            "synced_at": meta["synced_at"],
            "source_revision": digest(incoming),
            "target_revision": digest([revision(record), local]),
            "changes": changes,
        }

    async def ensure(self, user_id: str) -> AgentRecord:
        await self.authorized(user_id)
        if not self.settings or not self.settings.template_agent_id:
            await self.source()
        if user_id == self.settings.template_owner_id:
            # The template author already owns the configured instance.
            return await self.source()
        aid = identifier(
            user_id,
            self.settings.template_owner_id,
            self.settings.template_agent_id,
        )
        async with self.state.message_bus.acquire_lock(
            f"template:{user_id}:{aid}"
        ):
            existing = await self.storage.get_agent(user_id, aid)
            if existing:
                self.check_source(existing)
                return existing
            source, incoming, mcp_sources = await self.snapshot(user_id)
            data = AgentData(
                id=aid,
                workspace_id=identifier(aid, "workspace"),
                **incoming["agent"],
            )
            record = AgentRecord(
                id=aid, user_id=user_id, source="user", data=data
            )
            return await self.persist(
                record, source, incoming, incoming, mcp_sources, initial=True
            )

    async def update(
        self,
        record: AgentRecord,
        source_revision: str,
        target_revision: str,
        take_source: list[str],
    ) -> AgentRecord:
        async with self.state.message_bus.acquire_lock(
            f"template:{record.user_id}:{record.id}"
        ):
            record = await self.storage.get_agent(record.user_id, record.id)
            if record is None:
                raise HTTPException(404, "智能体不存在。")
            meta = self.check_source(record)
            async with idle_workspace(
                self.storage,
                self.state.message_bus,
                record.user_id,
                record.data.workspace_id,
            ):
                source, incoming, mcp_sources = await self.snapshot(
                    record.user_id
                )
                local = await self.current(record)
                if source_revision != digest(
                    incoming
                ) or target_revision != digest([revision(record), local]):
                    raise HTTPException(
                        409, "来源或个人配置已变化，请重新检查更新。"
                    )
                changes = []
                merged = merge_documents(
                    meta["baseline"],
                    local,
                    incoming,
                    take_source,
                    changes=changes,
                )
                if not set(take_source) <= {
                    c["path"] for c in changes if c["conflict"]
                }:
                    raise HTTPException(422, "只能选择预览中的冲突字段。")
                return await self.persist(
                    record,
                    source,
                    incoming,
                    merged,
                    mcp_sources,
                    initial=False,
                )

    async def persist(
        self,
        record: AgentRecord,
        source: AgentRecord,
        incoming: dict[str, Any],
        merged: dict[str, Any],
        mcp_sources: dict[str, str],
        *,
        initial: bool,
    ) -> AgentRecord:
        expected = revision(record)
        data = AgentData(
            id=record.id,
            workspace_id=record.data.workspace_id,
            **merged["agent"],
        )
        for rid in credential_ids(data):
            await self.state.resource_access_service.resolve_credential(
                record.user_id, rid
            )
        for rid in knowledge_ids(data):
            await self.state.resource_access_service.resolve_knowledge_base(
                record.user_id, rid
            )
        data.template_sync = {
            "source_owner_id": source.user_id,
            "source_agent_id": source.id,
            "source_name": source.data.name,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "baseline": incoming,
            "mcp_sources": mcp_sources,
        }
        backups = []
        committed = False
        try:
            for kind, singular, cls in (
                ("skills", "skill", SkillRecord),
                ("mcps", "mcp", MCPRecord),
            ):
                for rid, payload in merged[kind].items():
                    old = await getattr(self.storage, "get_" + singular)(
                        record.user_id, rid
                    )
                    if old and resource_document(kind, old) == payload:
                        continue
                    if kind == "skills":
                        self.state.resource_service.skill_store.path(
                            payload["artifact_id"]
                        )
                    resource = cls(id=rid, user_id=record.user_id, **payload)
                    holder = await getattr(
                        self.storage, "get_" + singular + "_by_name"
                    )(record.user_id, resource.name)
                    if holder is not None and holder.id != rid:
                        raise HTTPException(
                            409,
                            f"已有同名 {singular}：{resource.name}，请先重命名个人资源再同步。",
                        )
                    backups.append((singular, rid, old, payload))
                    await getattr(self.storage, "upsert_" + singular)(
                        record.user_id, resource
                    )
            data.resource_config = await ResourceConfigService(
                self.storage,
                self.state.resource_access_service,
            ).validate_config(
                record.user_id,
                record.user_id,
                data.resource_config,
                data.resource_config,
            )
            # A source edit during installation requires another explicit preview.
            _, latest, _ = await self.snapshot(record.user_id)
            if digest(latest) != digest(incoming):
                raise HTTPException(409, "来源配置已变化，请重新检查更新。")
            if initial:
                record.data = data
                await self.storage.upsert_agent(record.user_id, record)
            else:

                def mutate(latest_record: AgentRecord) -> None:
                    if revision(latest_record) != expected:
                        raise HTTPException(
                            409, "个人配置已变化，请重新检查更新。"
                        )
                    latest_record.data = data

                record = await self.storage.mutate_agent(
                    record.user_id, record.id, mutate
                )
            committed = True
            for singular, rid, _, _ in backups:
                if singular == "mcp":
                    try:
                        await self.state.resource_service.invalidate(
                            record.user_id, rid
                        )
                    except Exception:
                        # New runs also compare the configuration fingerprint.
                        logger.exception(
                            "Template MCP pool retirement failed: resource_id=%s",
                            rid,
                        )
            return record
        except BaseException:
            if committed:
                raise

            async def rollback() -> None:
                for singular, rid, old, payload in reversed(backups):
                    current = await getattr(self.storage, "get_" + singular)(
                        record.user_id, rid
                    )
                    if (
                        current is None
                        or resource_document(singular + "s", current)
                        != payload
                    ):
                        # Never roll back a newer independent resource edit.
                        continue
                    if old:
                        await getattr(self.storage, "upsert_" + singular)(
                            record.user_id, old
                        )
                    else:
                        await getattr(self.storage, "delete_" + singular)(
                            record.user_id, rid
                        )

            await asyncio.shield(rollback())
            raise

    async def mcp_headers(
        self, user_id: str, agent_id: str, record: MCPRecord
    ) -> dict[str, str]:
        """Inject the verified user's token only into an unchanged approved MCP."""
        agent = await self.storage.get_agent(user_id, agent_id)
        meta = agent.data.template_sync if agent else None
        if not meta or record.id not in meta.get("mcp_sources", {}):
            return {}
        self.check_source(agent)
        source = await self.source()
        rid = meta["mcp_sources"][record.id]
        if (
            rid not in source.data.resource_config.mcps
            or not source.data.resource_config.mcps[rid].enabled
        ):
            raise HTTPException(403, "来源 MCP 使用权限已撤销。")
        approved = meta["baseline"]["mcps"][record.id]["client"]
        upstream = await self.storage.get_mcp(source.user_id, rid)
        if (
            record.client.model_dump(mode="json") != approved
            or not upstream
            or upstream.client.mcp_config.url != approved["mcp_config"]["url"]
        ):
            raise HTTPException(
                409, "MCP 地址或配置已变化，请检查模板更新后重试。"
            )
        token = await self.auth.store.nvwa_token(user_id)
        if not token:
            raise HTTPException(
                401, "女娲业务凭证缺失或已过期，请从女娲重新登录。"
            )
        prefix = self.settings.mcp_authorization_prefix
        if prefix and not token.lower().startswith(prefix.lower()):
            token = prefix + token
        return {"Authorization": token}
