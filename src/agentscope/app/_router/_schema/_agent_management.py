"""Explicit ownership and copy selections for the management surface."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from ._agent import CreateAgentRequest
from ...storage._model._agent_settings import AgentDefaults, AgentMemoryConfig


class SaveManagedAgent(CreateAgentRequest):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    memory_config: AgentMemoryConfig = Field(default_factory=AgentMemoryConfig)
    revision: str | None = None


class CopyAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[\w-]+$")
    revision: str
    target_user_id: str | None = None
    source_session_id: str | None = None
    workspace_id: str | None = None
    skills: list[str] | None = None
    mcps: list[str] | None = None
    skill_mode: Literal["link", "copy"] = "link"
    mcp_mode: Literal["link", "copy"] = "link"
    ai_mode: Literal["link", "copy"] = "link"
    ai_fields: (
        list[
            Literal[
                "chat_model_config",
                "fallback_chat_model_config",
                "tts_model_config",
                "realtime_model_config",
                "retrieval_model_config",
            ]
        ]
        | None
    ) = None
    knowledge_base_ids: list[str] | None = None
    memory: bool = False
    files: list[str] = Field(default_factory=list, max_length=1000)


class TemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    take_source: list[str] = Field(default_factory=list, max_length=2000)


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str = Field(max_length=1024 * 1024)
    revision: str | None = None
    workspace_id: str | None = None
