"""Independent agent management, with server-enforced owner scope."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from uuid import uuid4

from ..deps import get_current_user_id
from .._service._agent_management import AgentManagement, view
from .._service._agent_files import (
    digest,
    idle_workspace,
    list_files,
    read_bytes,
    safe_path,
)
from ._schema._agent_management import (
    CopyAgentRequest,
    MemoryWrite,
    SaveManagedAgent,
    TemplateUpdateRequest,
)
from ._schema._hub_skill import SkillView
from ._schema._hub_mcp import MCPView

management_router = APIRouter(
    prefix="/agent-management", tags=["agent-management"]
)


def management(request: Request, user_id=Depends(get_current_user_id)):
    return AgentManagement(request, user_id)


@management_router.get("")
async def listing(owner_id: str | None = None, service=Depends(management)):
    users = await service.users()
    owners = (
        [await service.owner(owner_id)]
        if owner_id
        else [u["id"] for u in users]
    )
    agents = []
    for owner in owners:
        agents.extend(
            view(r)
            for r in await service.storage.list_agents(
                owner, include_team=True
            )
        )
    agents.sort(key=lambda r: r["updated_at"], reverse=True)
    return {
        "agents": agents,
        "users": users,
        "total": len(agents),
        "admin": service.admin,
    }


@management_router.get("/resources")
async def resources(owner_id: str | None = None, service=Depends(management)):
    owner = await service.owner(owner_id)
    storage = service.storage
    from ..access import ResourceKind

    credentials = await service.access.list_resource(
        owner, ResourceKind.CREDENTIAL
    )
    knowledge = await service.access.list_resource(
        owner, ResourceKind.KNOWLEDGE_BASE
    )
    return {
        "skills": [
            SkillView.from_record(r) for r in await storage.list_skills(owner)
        ],
        "mcps": [
            MCPView.from_record(r) for r in await storage.list_mcps(owner)
        ],
        "credentials": [
            {
                "id": r.id,
                "name": r.data.get("name", r.id),
                "type": r.data.get("type"),
                "editable": r.editable,
            }
            for r in credentials
        ],
        "knowledge_bases": [{"id": r.id, "name": r.name} for r in knowledge],
    }


@management_router.post("", status_code=201)
async def create(
    body: SaveManagedAgent,
    owner_id: str | None = None,
    service=Depends(management),
):
    return await service.save(owner_id, body)


@management_router.get("/{agent_id}")
async def detail(
    agent_id: str, owner_id: str | None = None, service=Depends(management)
):
    record = await service.agent(owner_id, agent_id)
    return {"agent": view(record), "sessions": await service.sessions(record)}


@management_router.patch("/{agent_id}")
async def update(
    agent_id: str,
    body: SaveManagedAgent,
    owner_id: str | None = None,
    service=Depends(management),
):
    return await service.save(owner_id, body, agent_id)


@management_router.delete("/{agent_id}", status_code=204)
async def delete(
    agent_id: str, owner_id: str | None = None, service=Depends(management)
):
    record = await service.agent(owner_id, agent_id)
    if not await service.state.session_service.delete_agent(
        record.user_id, agent_id
    ):
        raise HTTPException(404, "智能体不存在。")


@management_router.get("/{agent_id}/copy-preview")
async def preview(
    agent_id: str,
    owner_id: str | None = None,
    source_session_id: str | None = None,
    workspace_id: str | None = None,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    return await service.preview(record, source_session_id, workspace_id)


@management_router.post("/{agent_id}/copy", status_code=201)
async def copy(
    agent_id: str,
    body: CopyAgentRequest,
    owner_id: str | None = None,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    return await service.copy(record, body)


@management_router.get("/{agent_id}/template-preview")
async def template_preview(
    agent_id: str,
    owner_id: str | None = None,
    service=Depends(management),
):
    from .._service._agent_template import AgentTemplateService

    record = await service.agent(owner_id, agent_id)
    return await AgentTemplateService(service.state).preview(record)


@management_router.post("/{agent_id}/template-update")
async def template_update(
    agent_id: str,
    body: TemplateUpdateRequest,
    owner_id: str | None = None,
    service=Depends(management),
):
    from .._service._agent_template import AgentTemplateService

    record = await service.agent(owner_id, agent_id)
    saved = await AgentTemplateService(service.state).update(
        record,
        body.source_revision,
        body.target_revision,
        body.take_source,
    )
    return view(saved)


@management_router.get("/{agent_id}/files")
async def files(
    agent_id: str,
    owner_id: str | None = None,
    workspace_id: str | None = None,
    memory: bool = True,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    workspace = await service.workspace(record, workspace_id)
    return {
        "workspace_id": workspace.workspace_id,
        "files": await list_files(workspace, memory=memory),
    }


def memory_path(path):
    if not path.startswith("Memory/") or not path.lower().endswith(".md"):
        raise HTTPException(
            422, "记忆文件必须是 Memory 目录下的 Markdown 文件。"
        )


@management_router.get("/{agent_id}/memory")
async def read_memory(
    agent_id: str,
    path: str = Query(...),
    owner_id: str | None = None,
    workspace_id: str | None = None,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    memory_path(path)
    workspace = await service.workspace(record, workspace_id)
    content = await read_bytes(workspace, path, 1024 * 1024)
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise HTTPException(422, "记忆文件不是 UTF-8 文本。") from exc
    return {"content": text, "revision": digest(content)}


@management_router.put("/{agent_id}/memory")
async def write_memory(
    agent_id: str,
    body: MemoryWrite,
    owner_id: str | None = None,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    memory_path(body.path)
    workspace = await service.workspace(record, body.workspace_id)
    async with idle_workspace(
        service.storage,
        service.state.message_bus,
        record.user_id,
        workspace.workspace_id,
    ):
        path = await safe_path(workspace, body.path)
        backend = workspace.get_backend()
        if await backend.file_exists(path):
            current = await read_bytes(workspace, body.path, 1024 * 1024)
            if body.revision != digest(current):
                raise HTTPException(409, "记忆已变化，请重新加载后编辑。")
        elif body.revision is not None:
            raise HTTPException(409, "记忆文件已删除，请重新加载。")
        data = body.content.encode("utf-8")
        if len(data) > 1024 * 1024:
            raise HTTPException(413, "记忆文件不能超过 1 MiB。")
        await backend.write_file(path, data)
    return {"revision": digest(data)}


@management_router.delete("/{agent_id}/memory", status_code=204)
async def delete_memory(
    agent_id: str,
    path: str = Query(...),
    revision: str = Query(...),
    owner_id: str | None = None,
    workspace_id: str | None = None,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    memory_path(path)
    workspace = await service.workspace(record, workspace_id)
    async with idle_workspace(
        service.storage,
        service.state.message_bus,
        record.user_id,
        workspace.workspace_id,
    ):
        content = await read_bytes(workspace, path, 1024 * 1024)
        if digest(content) != revision:
            raise HTTPException(409, "记忆已变化，请刷新后重试。")
        await workspace.get_backend().delete_path(
            await safe_path(workspace, path)
        )


@management_router.post("/{agent_id}/memory/clear", status_code=204)
async def clear_memory(
    agent_id: str,
    owner_id: str | None = None,
    workspace_id: str | None = None,
    service=Depends(management),
):
    record = await service.agent(owner_id, agent_id)
    workspace = await service.workspace(record, workspace_id)
    async with idle_workspace(
        service.storage,
        service.state.message_bus,
        record.user_id,
        workspace.workspace_id,
    ):
        rows = await list_files(workspace, memory=True)
        paths = [await safe_path(workspace, row["path"]) for row in rows]
        for path in paths:
            await workspace.get_backend().delete_path(path)


async def get_managed_resource_owner(
    request: Request, user_id=Depends(get_current_user_id)
):
    """Scope resource editors without changing the authenticated identity."""
    return await AgentManagement(request, user_id).owner(
        request.query_params.get("resource_owner")
    )


class CredentialInput(BaseModel):
    data: dict


def masked_credential(record):
    from ...credential import CredentialFactory

    return CredentialFactory.from_dict(record.data).model_dump(mode="json")


@management_router.get("/resources/credential/{credential_id}")
async def credential_detail(
    credential_id: str,
    owner_id: str | None = None,
    service=Depends(management),
):
    owner = await service.owner(owner_id)
    record = await service.storage.get_credential(owner, credential_id)
    if not record:
        raise HTTPException(404, "AI 资源不存在或不可编辑。")
    return {"data": masked_credential(record)}


@management_router.post("/resources/credential", status_code=201)
async def credential_create(
    body: CredentialInput,
    owner_id: str | None = None,
    service=Depends(management),
):
    from ._credential import _validate_credential

    owner = await service.owner(owner_id, creating=True)
    payload = {**body.data, "id": uuid4().hex}
    saved = await service.storage.upsert_credential(
        owner, _validate_credential(payload)
    )
    return {"id": saved}


@management_router.patch("/resources/credential/{credential_id}")
async def credential_edit(
    credential_id: str,
    body: CredentialInput,
    owner_id: str | None = None,
    service=Depends(management),
):
    from ._credential import _validate_credential

    owner = await service.owner(owner_id)
    record = await service.storage.get_credential(owner, credential_id)
    if not record:
        raise HTTPException(404, "AI 资源不存在或不可编辑。")
    masked = masked_credential(record)
    payload = {**record.data, **body.data, "id": credential_id}
    if payload.get("type") != record.data.get("type"):
        raise HTTPException(422, "修改提供商类型请创建新 AI 资源。")
    for key, value in body.data.items():
        if value == "**********" and masked.get(key) == value:
            payload[key] = record.data[key]
    await service.storage.upsert_credential(
        owner, _validate_credential(payload)
    )
    return {"id": credential_id}


@management_router.post("/resources/mcp/{mcp_id}/test")
async def mcp_test(
    mcp_id: str, owner_id: str | None = None, service=Depends(management)
):
    owner = await service.owner(owner_id)
    record = await service.storage.get_mcp(owner, mcp_id)
    if not record:
        raise HTTPException(404, "MCP 不存在。")
    from ..resources._environment import resolve_environment
    from ...mcp import MCPClient

    try:
        client = MCPClient.model_validate(
            resolve_environment(record.client.model_dump(mode="json"))
        )
        async with asyncio.timeout(20):
            try:
                await client.connect()
                tools = await client.list_raw_tools()
                return {"ok": True, "tools": [t.name for t in tools]}
            finally:
                await client.close()
    except Exception as exc:
        raise HTTPException(
            422, "MCP 连接失败，请检查服务地址、启动参数和凭据。"
        ) from exc


@management_router.get("/resources/{kind}/{resource_id}/usage")
async def resource_usage(
    kind: str,
    resource_id: str,
    owner_id: str | None = None,
    service=Depends(management),
):
    owner = await service.owner(owner_id)
    agents = []
    if kind not in {"skill", "mcp", "credential"}:
        raise HTTPException(404, "资源类型不存在。")
    for record in await service.storage.list_agents(owner, include_team=True):
        used = (
            resource_id in record.data.resource_config.skills
            if kind == "skill"
            else (
                resource_id in record.data.resource_config.mcps
                if kind == "mcp"
                else any(
                    c and c.credential_id == resource_id
                    for c in (
                        [
                            getattr(record.data.defaults, k)
                            for k in (
                                "chat_model_config",
                                "fallback_chat_model_config",
                                "tts_model_config",
                                "realtime_model_config",
                            )
                        ]
                        if record.data.defaults
                        else []
                    )
                    + [
                        getattr(
                            record.data.memory_config,
                            "retrieval_model_config",
                            None,
                        )
                    ]
                )
            )
        )
        if not used:
            for session in await service.storage.list_sessions(
                owner, record.id
            ):
                configs = [
                    getattr(session.config, k)
                    for k in (
                        "chat_model_config",
                        "fallback_chat_model_config",
                        "tts_model_config",
                        "realtime_model_config",
                    )
                ]
                used = (
                    resource_id in session.config.resource_config.skills
                    if kind == "skill"
                    else (
                        resource_id in session.config.resource_config.mcps
                        if kind == "mcp"
                        else any(
                            c and c.credential_id == resource_id
                            for c in configs
                        )
                    )
                )
                if used:
                    break
        if used:
            agents.append({"id": record.id, "name": record.data.name})
    return {"agents": agents}


@management_router.post(
    "/resources/{kind}/{resource_id}/copy", status_code=201
)
async def resource_copy(
    kind: str,
    resource_id: str,
    owner_id: str | None = None,
    service=Depends(management),
):
    from datetime import datetime
    from ...credential import CredentialFactory
    from .._service._agent_management import clone_skill_package

    owner = await service.owner(owner_id, creating=True)
    if kind not in {"skill", "mcp", "credential"}:
        raise HTTPException(404, "资源类型不存在。")
    resource = await getattr(service.storage, f"get_{kind}")(
        owner, resource_id
    )
    if resource is None:
        raise HTTPException(404, "资源不存在。")
    resource = resource.model_copy(deep=True)
    resource.id = uuid4().hex
    resource.created_at = resource.updated_at = datetime.now()
    if kind == "credential":
        resource.data.update(
            id=resource.id,
            name=f"{resource.data.get('name', 'AI')}-副本-{resource.id[:6]}",
        )
        await service.storage.upsert_credential(
            owner, CredentialFactory.from_dict(resource.data)
        )
        return {"id": resource.id}
    name = f"{resource.name[:60]}-copy-{resource.id[:8]}"
    if kind == "skill":
        resource.name = name
        await clone_skill_package(
            service.state.resource_service.skill_store, resource
        )
    else:
        resource.client = resource.client.model_copy(
            update={"name": name}, deep=True
        )
    await getattr(service.storage, f"upsert_{kind}")(owner, resource)
    return {"id": resource.id}
