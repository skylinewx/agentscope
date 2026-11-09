"""Owner-scoped agent administration and reproducible, isolated copying."""

import asyncio
import json
from datetime import datetime
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from pydantic import ValidationError

from ...credential import CredentialFactory
from ..storage import AgentData, AgentRecord, ResourceBinding
from ..storage._model._agent_settings import AgentDefaults, AgentMemoryConfig
from ..storage._agent_defaults import MODEL_FIELDS
from ._resource_config_service import ResourceConfigService
from ._agent_files import (
    MAX_TOTAL,
    digest,
    idle_workspace,
    list_files,
    read_bytes,
    safe_path,
)

COPY_NAMESPACE = UUID("d92f48de-6b25-493d-afb6-6dbd17ea57af")
AI_FIELDS = (*MODEL_FIELDS[:4], "retrieval_model_config")


def revision(record):
    return digest(record.model_dump_json().encode())


def view(record):
    data = record.model_dump(mode="json")
    data["data"].pop("copy_fingerprint", None)
    sync = data["data"].pop("template_sync", None)
    data["template"] = (
        {
            key: sync.get(key)
            for key in (
                "source_owner_id",
                "source_agent_id",
                "source_name",
                "synced_at",
            )
        }
        if sync
        else None
    )
    return {**data, "editable": True, "revision": revision(record)}


class AgentManagement:
    def __init__(self, request, viewer):
        self.state = request.app.state
        self.storage = self.state.storage
        self.access = self.state.resource_access_service
        self.viewer = viewer
        identity = getattr(request.state, "auth_user", None)
        self.admin = bool(identity and identity.get("role") == "admin")

    async def users(self):
        auth = getattr(self.state, "auth_service", None)
        if self.admin and auth:
            return [
                {
                    key: u.get(key, "")
                    for key in (
                        "id",
                        "username",
                        "display_name",
                        "status",
                    )
                }
                for u in (await auth.store.users()).values()
            ]
        if auth:
            user = await auth.store.user(self.viewer)
            if user:
                return [
                    {
                        key: user.get(key, "")
                        for key in (
                            "id",
                            "username",
                            "display_name",
                            "status",
                        )
                    }
                ]
        return [
            {
                "id": self.viewer,
                "username": self.viewer,
                "display_name": "",
                "status": "active",
            }
        ]

    async def owner(self, owner=None, *, creating=False):
        owner = owner or self.viewer
        if owner != self.viewer and not self.admin:
            raise HTTPException(403, "普通用户只能管理自己的智能体。")
        auth = getattr(self.state, "auth_service", None)
        if auth:
            user = await auth.store.user(owner)
            if not user:
                raise HTTPException(404, "所属用户不存在。")
            if creating and user["status"] != "active":
                raise HTTPException(409, "只能为启用的用户创建智能体。")
        return owner

    async def agent(self, owner, agent_id):
        owner = await self.owner(owner)
        record = await self.storage.get_agent(owner, agent_id)
        if record is None:
            raise HTTPException(404, "智能体不存在。")
        return record

    async def validate_defaults(self, owner, data):
        configs = (
            [(key, getattr(data.defaults, key)) for key in MODEL_FIELDS[:4]]
            if data.defaults
            else []
        )
        if data.memory_config:
            configs.append(
                (
                    "retrieval_model_config",
                    data.memory_config.retrieval_model_config,
                )
            )
        for key, config in configs:
            if config is not None:
                credential = await self.access.resolve_credential(
                    owner, config.credential_id
                )
                if (
                    key != "realtime_model_config"
                    and credential.data.get("type") != config.type
                ):
                    raise HTTPException(422, "模型类型与 AI 资源不匹配。")
                if not config.model.strip():
                    raise HTTPException(422, "请填写模型名称。")
                provider = CredentialFactory.from_dict(credential.data)
                try:
                    if key == "realtime_model_config":
                        from ._model import get_realtime_model

                        await get_realtime_model(owner, config, self.access)
                    elif key == "tts_model_config":
                        from ._tts_model import _resolve_tts_class

                        classes = provider.get_tts_model_classes()
                        if not classes:
                            raise ValueError("Unsupported TTS")
                        _resolve_tts_class(classes, config.model).Parameters(
                            **config.parameters
                        )
                    else:
                        provider.get_chat_model_class().Parameters(
                            **config.parameters
                        )
                except (ValueError, NotImplementedError) as exc:
                    raise HTTPException(
                        422,
                        "模型类型或参数不正确，请检查所选 AI 资源支持的能力。",
                    ) from exc
        if data.defaults and data.defaults.knowledge_config:
            for kb_id in data.defaults.knowledge_config.knowledge_base_ids:
                await self.access.resolve_knowledge_base(owner, kb_id)
            from ...middleware import RAGMiddleware

            try:
                RAGMiddleware.Parameters(
                    **data.defaults.knowledge_config.parameters
                )
            except ValidationError as exc:
                raise HTTPException(422, "知识库检索参数不正确。") from exc

    async def save(self, owner, body, agent_id=None):
        owner = await self.owner(owner, creating=agent_id is None)
        existing = await self.agent(owner, agent_id) if agent_id else None
        if existing and body.revision != revision(existing):
            raise HTTPException(409, "智能体已被修改，请刷新后重试。")
        payload = body.model_dump(exclude={"revision"}, exclude_none=True)
        if not body.name.strip():
            raise HTTPException(422, "名称不能为空。")
        data = AgentData.model_validate(payload)
        data.name = data.name.strip()
        data.resource_config = await ResourceConfigService(
            self.storage,
            self.access,
        ).validate_config(
            owner,
            owner,
            data.resource_config,
            existing.data.resource_config if existing else None,
        )
        await self.validate_defaults(owner, data)
        if existing:

            def mutate(latest):
                if revision(latest) != body.revision:
                    raise HTTPException(409, "智能体已被修改，请刷新后重试。")
                data.id = latest.data.id
                data.workspace_id = latest.data.workspace_id
                data.copy_fingerprint = latest.data.copy_fingerprint
                data.template_sync = latest.data.template_sync
                latest.data = data

            record = await self.storage.mutate_agent(owner, agent_id, mutate)
        else:
            data.workspace_id = uuid4().hex
            record = AgentRecord(id=data.id, user_id=owner, data=data)
            await self.storage.upsert_agent(owner, record)
        return view(record)

    async def sessions(self, record):
        rows = await self.storage.list_sessions(record.user_id, record.id)
        return [
            {
                "id": s.id,
                "name": s.config.name,
                "workspace_id": s.config.workspace_id,
                "defaults": {
                    **{k: getattr(s.config, k) for k in MODEL_FIELDS},
                    "permission_mode": s.state.permission_context.mode,
                },
            }
            for s in rows
        ]

    async def workspace(self, record, workspace_id=None):
        sessions = await self.storage.list_sessions(record.user_id, record.id)
        ids = {s.config.workspace_id for s in sessions}
        if record.data.workspace_id:
            ids.add(record.data.workspace_id)
        if workspace_id and workspace_id not in ids:
            raise HTTPException(404, "工作空间不属于该智能体。")
        if not workspace_id:
            if len(ids) > 1:
                raise HTTPException(
                    409, "此智能体有多个工作空间，请选择来源。"
                )
            workspace_id = next(iter(ids), None)
        session_id = next(
            (s.id for s in sessions if s.config.workspace_id == workspace_id),
            "",
        )
        return await self.state.workspace_manager.get_workspace(
            record.user_id,
            record.id,
            session_id,
            workspace_id,
        )

    async def selected_defaults(self, record, source_session_id):
        defaults = record.data.defaults or AgentDefaults()
        if source_session_id:
            session = await self.storage.get_session(
                record.user_id, record.id, source_session_id
            )
            if session is None or session.agent_id != record.id:
                raise HTTPException(404, "来源会话不存在。")
            defaults = AgentDefaults(
                **{key: getattr(session.config, key) for key in MODEL_FIELDS},
                permission_mode=session.state.permission_context.mode,
            )
        return defaults.model_copy(deep=True)

    async def preview(self, record, source_session_id=None, workspace_id=None):
        defaults = await self.selected_defaults(record, source_session_id)
        result = {
            "agent": view(record),
            "sessions": await self.sessions(record),
            "defaults": defaults,
            "skills": [],
            "mcps": [],
            "ai": [],
            "knowledge_bases": [],
            "memory": [],
            "files": [],
            "file_error": None,
        }
        for field, kind in (("skills", "skill"), ("mcps", "mcp")):
            for rid, binding in getattr(
                record.data.resource_config, field
            ).items():
                resource = await getattr(self.storage, f"get_{kind}")(
                    record.user_id, rid
                )
                available = resource is not None
                if kind == "skill" and available:
                    try:
                        self.state.resource_service.skill_store.path(
                            binding.artifact_id
                        )
                    except (ValueError, OSError, TypeError):
                        available = False
                result[field].append(
                    {
                        "id": rid,
                        "name": resource.name if resource else rid,
                        "enabled": binding.enabled,
                        "available": available,
                        "version": getattr(binding, "version", None)
                        or getattr(resource, "version", None),
                    }
                )
        for key in AI_FIELDS:
            config = (
                getattr(record.data.memory_config, key, None)
                if key == "retrieval_model_config"
                else getattr(defaults, key)
            )
            if config:
                try:
                    await self.access.resolve_credential(
                        record.user_id, config.credential_id
                    )
                    available = True
                except HTTPException:
                    available = False
                result["ai"].append(
                    {"id": key, "name": config.model, "available": available}
                )
        for kb_id in (
            defaults.knowledge_config.knowledge_base_ids
            if defaults.knowledge_config
            else []
        ):
            try:
                kb = await self.access.resolve_knowledge_base(
                    record.user_id, kb_id
                )
                item = {"id": kb_id, "name": kb.data.name, "available": True}
            except HTTPException:
                item = {"id": kb_id, "name": kb_id, "available": False}
            result["knowledge_bases"].append(item)
        try:
            workspace = await self.workspace(record, workspace_id)
            result["workspace_id"] = workspace.workspace_id
            result["memory"] = await list_files(workspace, memory=True)
            result["files"] = await list_files(workspace)
        except HTTPException as exc:
            result["file_error"] = exc.detail
        return result

    async def copy(self, record, body):
        target = await self.owner(
            body.target_user_id or record.user_id, creating=True
        )
        identity = uuid5(
            COPY_NAMESPACE, f"{self.viewer}:{body.request_id}"
        ).hex
        fingerprint = digest(
            json.dumps(
                {
                    "source": record.id,
                    "owner": record.user_id,
                    "request": body.model_dump(),
                },
                sort_keys=True,
            ).encode()
        )
        # Lock independent of target: reusing a token cannot race two owners.
        async with self.state.message_bus.acquire_lock(
            f"agent-copy:{identity}"
        ):
            existing = await self.storage.get_agent(target, identity)
            if existing:
                if existing.data.copy_fingerprint != fingerprint:
                    raise HTTPException(
                        409, "复制请求已使用，请重新打开复制窗口。"
                    )
                return view(existing)
            record = await self.agent(record.user_id, record.id)
            if body.revision != revision(record):
                raise HTTPException(409, "来源智能体已修改，请重新预览。")
            return await self._copy(
                record, target, identity, fingerprint, body
            )

    async def _copy(self, record, target, identity, fingerprint, body):
        owner = record.user_id
        data = record.data.model_copy(deep=True)
        data.template_sync = None
        data.id, data.name = identity, body.name.strip()
        if not data.name:
            raise HTTPException(422, "名称不能为空。")
        data.defaults = await self.selected_defaults(
            record, body.source_session_id
        )
        data.memory_config = data.memory_config or AgentMemoryConfig()
        data.workspace_id = uuid5(COPY_NAMESPACE, f"workspace:{identity}").hex
        data.copy_fingerprint = fingerprint
        created = []
        destination = None
        written = []

        def child_id(kind, old_id):
            return uuid5(COPY_NAMESPACE, f"{identity}:{kind}:{old_id}").hex

        try:
            for field, kind, mode in (
                ("skills", "skill", body.skill_mode),
                ("mcps", "mcp", body.mcp_mode),
            ):
                original = getattr(data.resource_config, field)
                selected = getattr(body, field)
                selected = list(original) if selected is None else selected
                if not set(selected) <= set(original):
                    raise HTTPException(
                        422, "只能复制来源智能体已关联的资源。"
                    )
                bindings = {}
                for rid in dict.fromkeys(selected):
                    resource = await getattr(self.storage, f"get_{kind}")(
                        owner, rid
                    )
                    if resource is None:
                        raise HTTPException(
                            409, "所选资源已失效，请取消选择或修复后重试。"
                        )
                    binding = original[rid].model_copy(deep=True)
                    binding.binding_id = ResourceBinding().binding_id
                    if kind == "skill":
                        try:
                            self.state.resource_service.skill_store.path(
                                binding.artifact_id
                            )
                        except (ValueError, OSError, TypeError) as exc:
                            raise HTTPException(
                                409, "所选 Skill 的绑定版本不可用。"
                            ) from exc
                    if mode == "copy" or target != owner:
                        resource = resource.model_copy(deep=True)
                        resource.id = child_id(kind, rid)
                        resource.user_id = target
                        resource.created_at = resource.updated_at = (
                            datetime.now()
                        )
                        new_name = (
                            f"{resource.name[:60]}-copy-{resource.id[:8]}"
                        )
                        if kind == "skill":
                            resource.name = new_name
                            resource.artifact_id = binding.artifact_id
                            resource.version = binding.version
                            resource.version_updated_at = (
                                binding.version_updated_at
                            )
                            await clone_skill_package(
                                self.state.resource_service.skill_store,
                                resource,
                            )
                            binding.artifact_id = resource.artifact_id
                        else:
                            resource.client = resource.client.model_copy(
                                update={"name": new_name}, deep=True
                            )
                        created.append((kind, resource.id))
                        await getattr(self.storage, f"upsert_{kind}")(
                            target, resource
                        )
                        rid = resource.id
                    bindings[rid] = binding
                setattr(data.resource_config, field, bindings)

            chosen = set(
                AI_FIELDS if body.ai_fields is None else body.ai_fields
            )
            credential_copies = {}
            for key in AI_FIELDS:
                parent = (
                    data.memory_config
                    if key == "retrieval_model_config"
                    else data.defaults
                )
                config = getattr(parent, key)
                if key not in chosen:
                    setattr(parent, key, None)
                    continue
                if not config:
                    continue
                credential = await self.access.resolve_credential(
                    owner, config.credential_id
                )
                if body.ai_mode == "copy" or target != owner:
                    # A shared credential can be used but cannot be exported.
                    if credential.user_id != owner:
                        raise HTTPException(
                            403,
                            "共享 AI 资源不能创建密钥副本，请沿用或取消选择。",
                        )
                    if credential.id not in credential_copies:
                        new_id = child_id("credential", credential.id)
                        payload = {
                            **credential.data,
                            "id": new_id,
                            "name": (
                                f"{credential.data.get('name', 'AI')}"
                                f"-副本-{new_id[:6]}"
                            ),
                        }
                        created.append(("credential", new_id))
                        await self.storage.upsert_credential(
                            target, CredentialFactory.from_dict(payload)
                        )
                        credential_copies[credential.id] = new_id
                    config.credential_id = credential_copies[credential.id]

            knowledge = data.defaults.knowledge_config
            if knowledge:
                ids = (
                    knowledge.knowledge_base_ids
                    if body.knowledge_base_ids is None
                    else body.knowledge_base_ids
                )
                if not set(ids) <= set(knowledge.knowledge_base_ids):
                    raise HTTPException(422, "只能选择来源已关联的知识库。")
                for kid in ids:
                    await self.access.resolve_knowledge_base(target, kid)
                knowledge.knowledge_base_ids = list(dict.fromkeys(ids))
            elif body.knowledge_base_ids:
                raise HTTPException(422, "来源智能体未关联知识库。")
            await self.validate_defaults(target, data)

            if body.memory or body.files:
                source = await self.workspace(record, body.workspace_id)
                async with idle_workspace(
                    self.storage,
                    self.state.message_bus,
                    owner,
                    source.workspace_id,
                ):
                    memory_rows = (
                        await list_files(source, memory=True)
                        if body.memory
                        else []
                    )
                    business_rows = (
                        await list_files(source) if body.files else []
                    )
                    if not set(body.files) <= {
                        r["path"] for r in business_rows
                    }:
                        raise HTTPException(422, "只能复制预览中的业务文件。")
                    paths = sorted(
                        {r["path"] for r in memory_rows} | set(body.files)
                    )
                    snapshot, total = {}, 0
                    for path in paths:
                        snapshot[path] = await read_bytes(source, path)
                        total += len(snapshot[path])
                        if total > MAX_TOTAL:
                            raise HTTPException(
                                413, "所选文件总计超过 50 MiB。"
                            )
                    # Catch changes outside service locks (external edits or
                    # new sessions) before publishing any destination.
                    for path, content in snapshot.items():
                        if digest(await read_bytes(source, path)) != digest(
                            content
                        ):
                            raise HTTPException(
                                409, "来源文件正在变化，请稍后重试。"
                            )
                    if body.memory and memory_rows != await list_files(
                        source, memory=True
                    ):
                        raise HTTPException(
                            409, "记忆文件已变化，请重新预览。"
                        )
                    destination = (
                        await self.state.workspace_manager.get_workspace(
                            target, identity, "", data.workspace_id
                        )
                    )
                    for path, content in snapshot.items():
                        resolved = await safe_path(destination, path)
                        written.append(resolved)
                        await destination.get_backend().write_file(
                            resolved, content
                        )
            # No visible agent exists until every selected dependency is ready.
            latest = await self.agent(owner, record.id)
            if revision(latest) != body.revision:
                raise HTTPException(
                    409, "复制期间来源配置已修改，请重新预览。"
                )
            result = AgentRecord(
                id=identity, user_id=target, source="user", data=data
            )
            await self.storage.upsert_agent(target, result)
            return view(result)
        except BaseException:

            async def cleanup():
                for kind, rid in reversed(created):
                    await getattr(self.storage, f"delete_{kind}")(target, rid)
                if destination:
                    for path in written:
                        await destination.get_backend().delete_path(path)
                    await self.state.workspace_manager.close(data.workspace_id)

            await asyncio.shield(cleanup())
            raise


async def clone_skill_package(store, record):
    """Preserve the pinned full package under an independent name."""
    import frontmatter

    source = frontmatter.loads(
        await store.read_file(record.artifact_id, "SKILL.md")
    )
    source["name"] = record.name
    package = await store.save_files(
        {"SKILL.md": frontmatter.dumps(source)}, record.artifact_id
    )
    record.artifact_id = package.artifact_id
    record.markdown = package.markdown
    record.description = package.description
