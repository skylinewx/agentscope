# -*- coding: utf-8 -*-
"""Persist scope selections; filesystem copies are never a selection source."""

from typing import Literal

from fastapi import HTTPException

from ..access import DenyAllResourceAccessPolicy
from ..storage import (
    AgentResourceConfig,
    SessionResourceConfig,
    ResourceBinding,
    SkillResourceBinding,
    ResourceOverride,
)
from ._access import ResourceAccessService
from ._resource_resolution import resolve_resources


class ResourceConfigService:
    """Shared validation and atomic selection operations."""

    def __init__(self, storage, access=None):
        self.storage = storage
        self.access = access or ResourceAccessService(
            storage,
            DenyAllResourceAccessPolicy(),
        )

    async def resource_record(self, viewer, owner, kind, resource_id):
        """Agent sharing alone does not grant use of its private resources."""
        return await self.access.try_resolve_bound_resource(
            viewer, owner, kind, resource_id
        )

    async def configs(self, user_id, agent_id, session_id):
        agent = await self.access.resolve_agent(user_id, agent_id)
        session = await self.storage.get_session(user_id, agent_id, session_id)
        if session is None:
            raise HTTPException(404, "Session not found.")
        return agent, session

    async def effective(self, user_id, agent_id, session_id):
        agent, session = await self.configs(user_id, agent_id, session_id)
        result = []
        for selection in resolve_resources(
            agent.data.resource_config, session.config.resource_config
        ):
            owner = user_id if selection.local else agent.user_id
            record = await self.resource_record(
                user_id, owner, selection.kind, selection.resource_id
            )
            result.append((selection, record))
        return result

    async def validate_config(
        self, viewer, owner, proposed, current=None, agent_config=None
    ):
        """Validate IDs, pin packages and retain binding generations."""
        proposed = proposed.model_copy(deep=True)
        current = current or type(proposed)()
        for kind, field in (("mcp", "mcps"), ("skill", "skills")):
            for resource_id, binding in getattr(proposed, field).items():
                previous = getattr(current, field).get(resource_id)
                record = await self.resource_record(
                    viewer, owner, kind, resource_id
                )
                if record is None:
                    # A deleted binding can remain displayed or be removed; it
                    # cannot be newly bound, enabled, or given a new package.
                    if previous is not None and binding == previous:
                        continue
                    raise HTTPException(
                        404, f"{kind.title()} resource not available."
                    )
                binding.binding_id = (
                    previous.binding_id
                    if previous
                    else ResourceBinding().binding_id
                )
                if kind == "skill":
                    if (
                        previous
                        and binding.artifact_id == previous.artifact_id
                    ):
                        binding.version = previous.version
                        binding.version_updated_at = (
                            previous.version_updated_at
                        )
                    else:
                        if binding.artifact_id not in (
                            None,
                            record.artifact_id,
                        ):
                            raise HTTPException(
                                422, "Select the current saved Skill package."
                            )
                        if not record.artifact_id:
                            raise HTTPException(
                                422, "Skill has no saved package."
                            )
                        binding.artifact_id = record.artifact_id
                        binding.version = record.version
                        binding.version_updated_at = record.version_updated_at
        if isinstance(proposed, SessionResourceConfig):
            agent_config = agent_config or AgentResourceConfig()
            for field, overrides in (
                ("mcps", proposed.mcp_overrides),
                ("skills", proposed.skill_overrides),
            ):
                for resource_id, override in overrides.items():
                    parent = getattr(agent_config, field).get(resource_id)
                    if (
                        parent is None
                        or parent.binding_id != override.binding_id
                    ):
                        raise HTTPException(
                            409, "Agent binding changed. Reload its resources."
                        )
        return proposed

    async def add_session_resource(
        self, user_id, agent_id, session_id, kind, record, *, prepared=None
    ):
        """Add a local selection without changing inherited defaults."""
        await self.configs(user_id, agent_id, session_id)
        binding = prepared or (
            ResourceBinding()
            if kind == "mcp"
            else SkillResourceBinding(
                artifact_id=record.artifact_id,
                version=record.version,
                version_updated_at=record.version_updated_at,
            )
        )
        if record.user_id != user_id:
            raise HTTPException(404, "Resource not found.")
        if kind == "skill" and not binding.artifact_id:
            raise HTTPException(422, "Skill has no saved package.")
        field = "mcps" if kind == "mcp" else "skills"
        override_field = (
            "mcp_overrides" if kind == "mcp" else "skill_overrides"
        )

        def mutate(session):
            config = session.config.resource_config
            getattr(config, field).setdefault(record.id, binding)
            getattr(config, override_field).pop(record.id, None)

        saved = await self.storage.mutate_session(
            user_id, agent_id, session_id, mutate
        )
        return (
            getattr(saved.config.resource_config, field)[record.id].binding_id
            == binding.binding_id
        )

    async def change_session_resource(
        self,
        user_id,
        agent_id,
        session_id,
        kind: Literal["mcp", "skill"],
        resource_id,
        enabled=None,
        *,
        remove=False,
    ):
        """True/false are explicit overrides; null resets inheritance."""
        agent, _ = await self.configs(user_id, agent_id, session_id)
        field = "mcps" if kind == "mcp" else "skills"
        override_field = (
            "mcp_overrides" if kind == "mcp" else "skill_overrides"
        )
        parent = getattr(agent.data.resource_config, field).get(resource_id)

        def mutate(session):
            config = session.config.resource_config
            additions, overrides = getattr(config, field), getattr(
                config, override_field
            )
            if remove:
                if resource_id not in additions:
                    raise HTTPException(
                        409,
                        "Inherited resources can be disabled, "
                        "not removed from the agent here.",
                    )
                additions.pop(resource_id)
                overrides.pop(resource_id, None)
            elif resource_id in additions:
                if enabled is None:
                    raise HTTPException(
                        409, "Local additions do not have an inherited switch."
                    )
                additions[resource_id].enabled = enabled
            elif parent:
                if enabled is None:
                    overrides.pop(resource_id, None)
                else:
                    overrides[resource_id] = ResourceOverride(
                        enabled=enabled, binding_id=parent.binding_id
                    )
            else:
                raise HTTPException(
                    404, "Resource is not bound to this scope."
                )

        return await self.storage.mutate_session(
            user_id, agent_id, session_id, mutate
        )
