# -*- coding: utf-8 -*-
"""Resource selections belong to agents and sessions, never the library."""

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...._utils._common import _generate_id


class ResourceBinding(BaseModel):
    """One explicit selection; its identity survives switch changes."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    binding_id: str = Field(default_factory=_generate_id)


class SkillResourceBinding(ResourceBinding):
    """A selection pinned to an immutable package, resolved by the server."""

    artifact_id: str | None = None
    version: str | None = None
    version_updated_at: datetime | None = None


class ResourceOverride(BaseModel):
    """A session switch targeting one incarnation of an agent binding.

    Rebinding an unbound resource creates a new binding id, so old session
    overrides cannot silently become active again.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    binding_id: str


class AgentResourceConfig(BaseModel):
    """Default selections for every conversation with this agent."""

    model_config = ConfigDict(extra="forbid")

    mcps: dict[str, ResourceBinding] = Field(default_factory=dict)
    skills: dict[str, SkillResourceBinding] = Field(default_factory=dict)


class SessionResourceConfig(AgentResourceConfig):
    """Explicit additions plus switches overriding inherited selections.

    ``mcps`` and ``skills`` are additions, not a copy of agent defaults.
    Absence of an override means inheritance; explicit false is retained.
    """

    mcp_overrides: dict[str, ResourceOverride] = Field(default_factory=dict)
    skill_overrides: dict[str, ResourceOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _distinct_additions_and_overrides(self) -> Self:
        for additions, overrides in (
            (self.mcps, self.mcp_overrides),
            (self.skills, self.skill_overrides),
        ):
            if additions.keys() & overrides.keys():
                raise ValueError(
                    "A resource cannot be both an addition and an override."
                )
        return self
