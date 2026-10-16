# -*- coding: utf-8 -*-
"""MCP router — the user's own library of installed MCPs.

This is the user-level collection an install lands in, distinct from
``/workspace/mcp``, which manages the MCPs of one session's workspace.
Installing from a hub only writes here; putting an MCP into a workspace
stays a separate, explicit act.
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from ..deps import (
    get_current_user_id,
    get_mcp_hubs,
    get_storage,
    get_resource_service,
)
from ..hub import MCPCard, MCPHubBase
from .._service import MCPRenderError, render_mcp, ResourceService
from ..storage import MCPRecord, StorageBase
from .._service._mcp_edit import (
    KEEP_SECRET,
    editable_mcp,
    restore_mcp_secrets,
)
from ...mcp import MCPClient
from ._schema import MCPView, UpdateMCPRequest
from ._schema._hub_mcp import CreateMCPRequest, MCPDetail

mcp_router = APIRouter(prefix="/mcp", tags=["mcp"])


@mcp_router.post("", status_code=status.HTTP_201_CREATED)
async def create_mcp(
    body: CreateMCPRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> MCPView:
    """Save a manually configured MCP without selecting any workspace."""
    if KEEP_SECRET in json.dumps(body.client.model_dump()):
        raise HTTPException(422, "A new MCP requires actual configuration.")
    if await storage.get_mcp_by_name(user_id, body.client.name):
        raise HTTPException(409, "An MCP with this name is already saved.")
    record = MCPRecord(
        user_id=user_id,
        client=body.client,
        description=body.description,
        version=body.version or uuid.uuid4().hex[:12],
    )
    try:
        await storage.upsert_mcp(user_id, record)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return MCPView.from_record(record)


@mcp_router.get("/{mcp_id}")
async def get_mcp(
    mcp_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> MCPDetail:
    """Return the caller's editable configuration with masked credentials."""
    record = await storage.get_mcp(user_id, mcp_id)
    if record is None:
        raise HTTPException(404, "MCP is not in your library.")
    return MCPDetail(
        **MCPView.from_record(record).model_dump(),
        client=editable_mcp(record),
    )


@mcp_router.get("")
async def list_mcps(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> list[MCPView]:
    """Return every MCP the user has installed, ordered by name."""
    records = await storage.list_mcps(user_id)
    return sorted(
        (MCPView.from_record(r) for r in records),
        key=lambda view: view.name,
    )


@mcp_router.patch("/{mcp_id}")
async def update_mcp(
    mcp_id: str,
    body: UpdateMCPRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> MCPView:
    """Rename, enable/disable, or re-key an installed MCP.

    Changing a secret is a re-render, not a field edit: an API key can
    sit anywhere in the config — inside a URL, a header, an env var — so
    the card's template plus the merged answers is the only thing that
    knows where to put it.

    Raises:
        `HTTPException`: 404 if the MCP is not in the caller's library,
            400 if the MCP has no card to re-render from, 409 if the new
            name is taken, or 422 if the name is not made of
            ``[a-zA-Z0-9_-]``.
    """
    record = await storage.get_mcp(user_id, mcp_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed MCP with id {mcp_id!r}.",
        )

    if body.client is not None and body.values is not None:
        raise HTTPException(422, "Send either client or values, not both.")
    if body.client is not None:
        try:
            record.client = MCPClient.model_validate(
                {
                    **restore_mcp_secrets(
                        record, body.client.model_dump(mode="json")
                    ),
                    "name": (
                        body.name
                        if body.name is not None
                        else body.client.name
                    ),
                }
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                422, "Invalid MCP name or configuration."
            ) from exc
        # Explicit configuration edits become user-managed definitions.
        record.hub_id = record.card_id = None
        record.card_snapshot = None
        record.values = {}
        record.display_name = None
        record.version = body.version or uuid.uuid4().hex[:12]
    elif body.values is not None:
        if record.card_snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This MCP has no saved card template. "
                    "Explicitly upgrade it, or edit the client "
                    "configuration directly."
                ),
            )
        card = MCPCard.model_validate(record.card_snapshot)

        # Merged, not replaced: the client sends only what changed, and
        # a write-only field the form did not echo back must survive.
        merged = {**record.values, **body.values}
        try:
            record.client = render_mcp(
                card,
                merged,
                body.name or record.client.name,
            )
        except MCPRenderError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            ) from e
        record.values = merged
        record.card_snapshot = card.model_dump(mode="json")
    elif body.name is not None:
        # ``MCPClient`` checks its name in ``model_post_init``, which a
        # plain attribute assignment skips; rebuild the client through
        # ``model_validate`` so an illegal name is refused here. Stored
        # as-is it would fail the same check on the next read, turning
        # every route that loads the library into a 500.
        try:
            record.client = MCPClient.model_validate(
                {**record.client.model_dump(), "name": body.name},
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Invalid MCP name. Use letters, numbers, "
                    "underscores or hyphens."
                ),
            ) from exc

    if body.description is not None:
        record.description = body.description
    if body.version is not None:
        record.version = body.version

    try:
        await storage.upsert_mcp(user_id, record)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e

    await resources.invalidate(user_id, record.id)
    return MCPView.from_record(record)


@mcp_router.post("/{mcp_id}/upgrade")
async def upgrade_mcp(
    mcp_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    hubs: dict[str, MCPHubBase] = Depends(get_mcp_hubs),
    resources: ResourceService = Depends(get_resource_service),
) -> MCPView:
    """Explicitly choose the user's hub resource configuration version."""
    record = await storage.get_mcp(user_id, mcp_id)
    if record is None:
        raise HTTPException(404, "MCP is not in your library.")
    try:
        hub = hubs.get(record.hub_id or "")
        if hub is None or record.card_id is None:
            raise ValueError("This MCP has no available upgrade source.")
        card = await hub.get_mcp(user_id, record.card_id)
        record.client = render_mcp(card, record.values, record.name)
        record.version = card.version
        record.card_snapshot = card.model_dump(mode="json")
        for name in (
            "display_name",
            "description",
            "tags",
            "author",
            "icon_url",
            "url",
        ):
            setattr(record, name, getattr(card, name))
        await storage.upsert_mcp(user_id, record)
        await resources.invalidate(user_id, record.id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return MCPView.from_record(record)


@mcp_router.delete("/{mcp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp(
    mcp_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> None:
    """Remove an MCP from the user's library.

    Workspace declarations are retained, but new runs cannot load an
    uninstalled resource. Every user-owned resource can be removed.
    """
    if not await storage.delete_mcp(user_id, mcp_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed MCP with id {mcp_id!r}.",
        )
    await resources.invalidate(user_id, mcp_id)
