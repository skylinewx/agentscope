# -*- coding: utf-8 -*-
"""Pure resolution shared by resource panels and runtime assembly."""

from dataclasses import dataclass
from typing import Literal

from ..storage._model._resource_config import (
    AgentResourceConfig,
    ResourceBinding,
    SessionResourceConfig,
)

ResourceType = Literal["mcp", "skill"]


@dataclass(frozen=True)
class EffectiveResource:
    """An effective selection; availability is checked separately."""

    kind: ResourceType
    resource_id: str
    binding: ResourceBinding
    binding_sources: list[str]
    agent_enabled: bool | None
    session_override: bool | None
    enabled: bool
    local: bool

    def scope_fields(self) -> dict:
        """Fields exposed by both resource list endpoints."""
        return {
            "binding_sources": self.binding_sources,
            "agent_enabled": self.agent_enabled,
            "session_override": self.session_override,
            "enabled": self.enabled,
            "effective_enabled": self.enabled,
            "binding_id": self.binding.binding_id,
        }


def resolve_resources(
    agent: AgentResourceConfig,
    session: SessionResourceConfig | None = None,
) -> list[EffectiveResource]:
    """Union explicit selections and apply session switches by identity."""
    session = session or SessionResourceConfig()
    result = []
    for kind, defaults, additions, overrides in (
        ("mcp", agent.mcps, session.mcps, session.mcp_overrides),
        ("skill", agent.skills, session.skills, session.skill_overrides),
    ):
        for resource_id in dict.fromkeys([*defaults, *additions]):
            inherited = defaults.get(resource_id)
            local = additions.get(resource_id)
            binding = local if local is not None else inherited
            override = overrides.get(resource_id)
            overridden = (
                override.enabled
                if inherited is not None
                and local is None
                and override is not None
                and override.binding_id == inherited.binding_id
                else None
            )
            result.append(
                EffectiveResource(
                    kind=kind,
                    resource_id=resource_id,
                    binding=binding,
                    binding_sources=(
                        (["agent"] if inherited is not None else [])
                        + (["session"] if local is not None else [])
                    ),
                    agent_enabled=(
                        inherited.enabled if inherited is not None else None
                    ),
                    session_override=overridden,
                    enabled=(
                        overridden
                        if overridden is not None
                        else binding.enabled
                    ),
                    local=local is not None,
                )
            )
    return result
