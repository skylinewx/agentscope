# -*- coding: utf-8 -*-
"""The builtin skill viewer tool."""
from typing import Any, Callable, Awaitable, List
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from ...exception import DeveloperOrientedException
from ...permission import (
    PermissionContext,
    PermissionDecision,
    PermissionBehavior,
)
from .._response import ToolChunk
from .._base import ToolBase, ToolMiddlewareBase
from ...skill import Skill
from ...message import TextBlock, ToolResultState
from ...state import AgentState


class SkillViewer(ToolBase):
    """The builtin skill viewer tool."""

    name: str = "Skill"
    """The name of the skill viewer tool to the agent."""

    description = (
        "Retrieve a skill within the conversation. "
        "When users asks you to perform tasks, check if any of the available "
        "skills match. "
        "Skills provide specialized capabilities and domain knowledge. "
        "To read a referenced text file, use resource with its path relative "
        "to this skill (for example references/guide.md); do not reconstruct "
        "installation paths. On a missing reference, read the skill again "
        "and retry its current relative path before continuing dependent work."
    )
    """The tool description of the skill viewer tool to the agent."""

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The exact name of the skill to view. ",
            },
            "resource": {
                "type": "string",
                "description": "Optional UTF-8 text file path relative to the "
                "active skill directory. Omit to read the skill instructions.",
            },
        },
        "required": ["skill"],
    }
    """The input schema of the skill viewer tool."""

    is_concurrency_safe: bool = True
    """The skill viewer is concurrency safe."""

    is_external_tool: bool = False
    """The skill viewer is not an external tool."""

    is_state_injected: bool = True
    """The skill viewer require state injection to access the activated tool
    group."""

    is_read_only: bool = True
    """The skill viewer is read-only."""

    is_mcp: bool = False
    """The skill viewer is not an MCP tool."""

    mcp_name: str | None = None
    """The skill viewer does not belong to any MCP server."""

    def __init__(
        self,
        get_skills_method: Callable[..., Awaitable[dict[str, Skill]]],
        middlewares: List[ToolMiddlewareBase] | None = None,
    ) -> None:
        """Initialize the skill viewer with the list of skills.

        Args:
            get_skills_method (`Callable[..., dict[str, Skill]]`):
                An async method that returns the current skills of the agent.
            middlewares (`List[ToolMiddlewareBase] | None`, optional):
                Tool middlewares wrapping the tool execution.
        """
        super().__init__(middlewares=middlewares)
        self._get_skills_method = get_skills_method

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """The skill viewer is always allowed to be called."""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="The skill viewer is always allowed to be called.",
        )

    async def call(
        self,
        skill: str,
        _agent_state: AgentState,
        resource: str | None = None,
    ) -> ToolChunk:
        """View the details of the skill with the given name.

        Args:
            skill (`str`):
                The name of the skill to be viewed.

        Returns:
            `ToolChunk`:
                The details of the skill.
        """
        if not isinstance(_agent_state, AgentState):
            raise DeveloperOrientedException(
                f"Expected AgentState but got {type(_agent_state)} "
                "instead for the Skill viewer tool.",
            )

        # View the activated skills
        skills = await self._get_skills_method(
            _agent_state.tool_context.activated_groups,
        )
        target_skill = skills.get(skill)
        if not target_skill:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=f"SkillNotFoundError: Skill '{skill}' "
                        f"not found. Available skills: {list(skills)[:50]}",
                    ),
                ],
                state=ToolResultState.ERROR,
            )

        if resource is None:
            return ToolChunk(content=[TextBlock(text=target_skill.markdown)])
        return self._read_resource(target_skill, resource)

    @staticmethod
    def _read_resource(skill: Skill, resource: str) -> ToolChunk:
        """Read bounded text within the currently activated skill, never cwd."""
        try:
            if not isinstance(resource, str) or not resource.strip():
                raise ValueError("resource must be a non-empty relative path")
            relative = PurePosixPath(resource.replace("\\", "/"))
            if (relative.is_absolute() or PureWindowsPath(resource).drive
                    or ".." in relative.parts or ":" in resource
                    or "\x00" in resource):
                raise ValueError("resource must stay within the skill directory")
            root = Path(skill.dir).resolve(strict=True)
            path = (root / str(relative)).resolve()
            if not path.is_relative_to(root):
                raise ValueError("resource resolves outside the skill directory")
            info = path.stat()
            limit = 256 * 1024
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("resource must be a regular text file up to 256 KiB")
            with path.open("rb") as source:
                raw = source.read(limit + 1)
            if len(raw) > limit or b"\x00" in raw:
                raise ValueError("resource must be UTF-8 text up to 256 KiB")
            text = raw.decode("utf-8-sig")
            return ToolChunk(content=[TextBlock(text=text)])
        except (OSError, ValueError, RuntimeError) as exc:
            # Do not expose arbitrary absolute paths or silently substitute a
            # similarly named file. The entry point is the recovery index.
            reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else type(exc).__name__
            return ToolChunk(
                content=[TextBlock(text=(
                    f"SkillResourceReadError: {reason}. Read Skill(skill={skill.name!r}) "
                    "again for current references, then retry Skill with the same "
                    "skill name and the exact relative resource path. The reference "
                    "was not read; do not treat dependent instructions as loaded."
                ))],
                state=ToolResultState.ERROR,
            )
