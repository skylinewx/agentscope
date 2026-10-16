# -*- coding: utf-8 -*-
# unittest.enterContext owns temporary resources.
# pylint: disable=protected-access, consider-using-with
# pylint: disable=too-many-public-methods
"""Resource policy integration tests without external servers or models."""

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from pydantic import PrivateAttr

from agentscope.app._service._resources import (
    ResourceService,
    ResourceToolkit,
    _MCPEntry,
    resource_toolkit_scope,
)
from agentscope.app.resources import LocalSkillStore
from agentscope.app.storage import (
    MCPRecord,
    SkillRecord,
    AgentRecord,
    AgentData,
    SessionRecord,
    SessionConfig,
)
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from agentscope.message import TextBlock, ToolResultState
from agentscope.workspace import LocalWorkspace


class MemoryResources:
    """Owner-scoped persistence with independent record copies."""

    def __init__(self) -> None:
        """Init fixture."""
        self.mcps, self.skills = {}, {}
        self.agents, self.sessions = {}, {}

    async def get_agent(self, user_id, agent_id):
        return self.agents.setdefault(
            (user_id, agent_id),
            AgentRecord(
                id=agent_id,
                user_id=user_id,
                data=AgentData(name=agent_id),
            ),
        ).model_copy(deep=True)

    async def get_session(self, user_id, agent_id, session_id):
        return self.sessions.setdefault(
            (user_id, agent_id, session_id),
            SessionRecord(
                id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                config=SessionConfig(workspace_id="test"),
            ),
        ).model_copy(deep=True)

    async def mutate_agent(self, user_id, agent_id, mutate):
        record = await self.get_agent(user_id, agent_id)
        mutate(record)
        self.agents[user_id, agent_id] = record.model_copy(deep=True)
        return record

    async def mutate_session(self, user_id, agent_id, session_id, mutate):
        record = await self.get_session(user_id, agent_id, session_id)
        mutate(record)
        self.sessions[user_id, agent_id, session_id] = record.model_copy(
            deep=True
        )
        return record

    async def list_mcps(self, user_id: Any) -> list[MCPRecord]:
        """Return independent MCP records for one user."""
        return [
            MCPRecord.model_validate(v)
            for (u, _), v in self.mcps.items()
            if u == user_id
        ]

    async def list_skills(self, user_id: Any) -> list[SkillRecord]:
        """Return independent Skill records for one user."""
        return [
            SkillRecord.model_validate(v)
            for (u, _), v in self.skills.items()
            if u == user_id
        ]

    async def get_mcp(self, user_id: Any, record_id: Any) -> MCPRecord | None:
        """Read one MCP record within the owner scope."""
        value = self.mcps.get((user_id, record_id))
        return MCPRecord.model_validate(value) if value else None

    async def get_skill(
        self, user_id: Any, record_id: Any
    ) -> SkillRecord | None:
        """Read one Skill record within the owner scope."""
        value = self.skills.get((user_id, record_id))
        return SkillRecord.model_validate(value) if value else None

    async def get_mcp_by_name(
        self, user_id: Any, name: Any
    ) -> MCPRecord | None:
        """Find a user MCP by its current library name."""
        return next(
            (r for r in await self.list_mcps(user_id) if r.name == name), None
        )

    async def get_skill_by_name(
        self, user_id: Any, name: Any
    ) -> SkillRecord | None:
        """Find a user Skill by its current library name."""
        return next(
            (r for r in await self.list_skills(user_id) if r.name == name),
            None,
        )

    async def upsert_mcp(self, user_id: Any, record: Any) -> None:
        """Persist an independent JSON snapshot of the MCP record."""
        self.mcps[user_id, record.id] = record.model_dump(mode="json")

    async def upsert_skill(self, user_id: Any, record: Any) -> None:
        """Persist an independent JSON snapshot of the Skill record."""
        self.skills[user_id, record.id] = record.model_dump(mode="json")


def mcp(name: Any = "echo", url: Any = "https://example.com/mcp") -> MCPClient:
    """Build a stateless HTTP MCP configuration for the fixture."""
    return MCPClient(
        name=name, is_stateful=False, mcp_config=HttpMCPConfig(url=url)
    )


class WaitingTool(ToolBase):
    """Controlled long call for snapshot/background lifecycle assertions."""

    name = "mcp__echo__wait"
    description = "Wait for the test."
    input_schema = {"type": "object", "properties": {}}
    is_read_only = True
    is_concurrency_safe = True
    is_mcp = True
    mcp_name = "echo"

    def __init__(self) -> None:
        """Init fixture."""
        super().__init__()
        self.started, self.finish = asyncio.Event(), asyncio.Event()

    async def check_permissions(
        self, *_args: Any, **_kwargs: Any
    ) -> PermissionDecision:
        """Permit the controlled test tool."""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW, message="Test allow."
        )

    async def call(self) -> ToolChunk:
        """Wait for explicit test completion before returning success."""
        self.started.set()
        await self.finish.wait()
        return ToolChunk(
            content=[TextBlock(text="done")], state=ToolResultState.SUCCESS
        )


class FakeMCP(MCPClient):
    """Transport stand-in recording the task that owns its lifecycle."""

    _test_tool: WaitingTool = PrivateAttr(default_factory=WaitingTool)
    _connected_by: object = PrivateAttr(default=None)
    _closed_by: object = PrivateAttr(default=None)

    async def connect(self) -> None:
        """Initialize the controlled fixture transport."""
        self._connected_by = asyncio.current_task()
        self._is_connected = True

    async def close(self, ignore_errors: Any = True) -> None:
        """Record closure of the fixture transport."""
        self._closed_by = asyncio.current_task()
        self._is_connected = False

    async def list_tools(self) -> list[ToolBase]:
        """Return the controlled tool fixture."""
        return [self._test_tool]


class ResourceServiceTest(IsolatedAsyncioTestCase):
    """Resource lifecycle behavior through real workspaces."""

    async def asyncSetUp(self) -> None:
        """Create explicitly owned resources and an isolated workspace."""
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.skill_path = self.root / "uploads" / "analysis"
        self.skill_path.mkdir(parents=True)
        (self.skill_path / "SKILL.md").write_text(
            "---\nname: analysis\ndescription: Analyse data.\n---\n"
            "Use scripts/run.py.\n",
            encoding="utf-8",
        )
        (self.skill_path / "scripts").mkdir()
        (self.skill_path / "scripts" / "run.py").write_text(
            "print('v1')", encoding="utf-8"
        )
        self.storage = MemoryResources()
        self.store = LocalSkillStore(str(self.root / "artifacts"))
        self.service = ResourceService(self.storage, self.store)
        self.addAsyncCleanup(self.service.close)
        self.workspace = LocalWorkspace(workdir=str(self.root / "workspace"))
        await self.workspace.initialize()
        self.addAsyncCleanup(self.workspace.close)
        self.clients = []

        def create(spec: Any, **_kwargs: Any) -> FakeMCP:
            """Create fixture."""
            client = FakeMCP.model_validate(spec.model_dump(mode="json"))
            self.clients.append(client)
            return client

        self.workspace.create_mcp_client = create
        package = await self.store.capture_directory(str(self.skill_path))
        record = MCPRecord(user_id="alice", client=mcp(), version="1")
        skill = SkillRecord(
            user_id="alice",
            name=package.name,
            description=package.description,
            markdown=package.markdown,
            artifact_id=package.artifact_id,
            version="1",
        )
        await self.storage.upsert_mcp("alice", record)
        await self.storage.upsert_skill("alice", skill)
        await self.service.equip_mcp(
            "alice", self.workspace, "agent", "session", record
        )
        await self.service.equip_skill(
            "alice", self.workspace, "agent", "session", skill
        )

    async def set_enabled(self, record, enabled):
        kind = "mcp" if isinstance(record, MCPRecord) else "skill"
        session = await self.storage.get_session("alice", "agent", "session")
        field = "mcps" if kind == "mcp" else "skills"
        if record.id in getattr(session.config.resource_config, field):
            await self.service.change_session_resource(
                "alice", "agent", "session", kind, record.id, enabled
            )

    async def records(self) -> tuple[MCPRecord, SkillRecord]:
        """Read the seeded MCP and Skill records."""
        return (await self.storage.list_mcps("alice"))[0], (
            await self.storage.list_skills("alice")
        )[0]

    async def snapshot(
        self, agent: Any = "agent", session: Any = "session"
    ) -> Any:
        """Assemble a leased toolkit and register its cleanup."""
        tools, skills, entries = await self.service.snapshot(
            "alice", self.workspace, agent, session
        )
        toolkit = ResourceToolkit(
            entries=entries, tools=tools, skills_or_loaders=skills
        )
        self.addAsyncCleanup(toolkit.aclose)
        return toolkit, tools, skills

    async def test_model_skill_name_survives_selected_package_upgrade(self):
        from agentscope.tool._builtin._skill import SkillViewer
        from agentscope.state import AgentState
        from agentscope.message import ToolResultState

        _, previous = await self.records()
        old = (await self.snapshot())[2][0]
        await self.upgraded_service()
        await self.publish_updates()
        _, current = await self.records()
        await self.service.equip_skill(
            "alice", self.workspace, "agent", "session", current, upgrade=True
        )
        toolkit, _, skills = await self.snapshot()
        self.assertEqual([s.name for s in skills], [previous.name])
        self.assertNotEqual(skills[0].dir, old.dir)
        self.assertIn(
            "v2", Path(skills[0].dir, "scripts/run.py").read_text("utf-8")
        )
        viewer = SkillViewer(toolkit._get_available_skills)
        result = await viewer.call(previous.name, AgentState())
        self.assertNotEqual(result.state, ToolResultState.ERROR)
        self.assertEqual(result.content[0].text, skills[0].markdown)
        missing = await viewer.call("not-equipped", AgentState())
        self.assertEqual(missing.state, ToolResultState.ERROR)
        self.assertIn(previous.name, missing.content[0].text)

    async def test_restart_preserves_choices_and_keeps_other_users_empty(
        self,
    ) -> None:
        """Restart preserves choices and leaves empty users empty."""
        record, skill = await self.records()
        record.version = "old"
        await self.set_enabled(record, False)
        await self.set_enabled(skill, False)
        await self.storage.upsert_mcp("alice", record)
        await self.storage.upsert_skill("alice", skill)
        before = dict(self.storage.mcps), dict(self.storage.skills)
        restarted = ResourceService(self.storage, self.store)
        self.addAsyncCleanup(restarted.close)
        await restarted.list_mcps("alice", self.workspace, "agent", "session")
        await restarted.list_skills(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(
            await restarted.list_mcps(
                "bob", self.workspace, "agent", "session"
            ),
            [],
        )
        self.assertEqual(
            await restarted.list_skills(
                "bob", self.workspace, "agent", "session"
            ),
            [],
        )
        self.assertEqual((self.storage.mcps, self.storage.skills), before)
        saved, saved_skill = await self.records()
        self.assertEqual(saved.id, record.id)
        self.assertEqual(saved.version, "old")
        self.assertFalse(
            (
                await restarted.list_mcps(
                    "alice", self.workspace, "agent", "session"
                )
            )[0]["enabled"]
        )
        self.assertFalse(
            (
                await restarted.list_skills(
                    "alice", self.workspace, "agent", "session"
                )
            )[0]["enabled"]
        )

    async def test_listing_never_connects_and_redacts_configuration(
        self,
    ) -> None:
        """Listing never connects and redacts configuration."""
        listing = await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(self.clients, [])
        self.assertEqual(listing[0]["status"], "not_loaded")
        self.assertNotIn("mcp_config", listing[0])
        self.assertIn("resource_id", listing[0])

    async def test_disabled_selections_remain_present_but_unavailable(
        self,
    ) -> None:
        """Disabled selections remain present but unavailable."""
        record, skill = await self.records()
        await self.set_enabled(record, False)
        await self.set_enabled(skill, False)
        await self.storage.upsert_mcp("alice", record)
        await self.storage.upsert_skill("alice", skill)
        _, tools, available = await self.snapshot()
        self.assertFalse(tools)
        self.assertFalse(available)
        self.assertFalse(self.clients)
        copies = await self.service.list_skills(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(copies[0]["status"], "disabled")
        self.assertTrue(Path(copies[0]["dir"], "scripts/run.py").exists())

    async def test_disabled_scope_equipping_is_idempotent(self):
        record, skill = await self.records()
        await self.set_enabled(record, False)
        await self.set_enabled(skill, False)
        await self.service.equip_mcp(
            "alice", self.workspace, "agent", "session", record
        )
        await self.service.equip_skill(
            "alice", self.workspace, "agent", "session", skill
        )
        _, tools, skills = await self.snapshot()
        self.assertEqual((tools, skills), ([], []))

    async def test_snapshots_finish_while_new_runs_observe_disable(
        self,
    ) -> None:
        """Snapshots finish while new runs observe disable."""
        toolkit, _, _ = await self.snapshot()
        record, _ = await self.records()
        await self.set_enabled(record, False)
        await self.storage.upsert_mcp("alice", record)
        await self.service.invalidate("alice", record.id)
        self.assertTrue(self.clients[0].is_connected)
        _, new_tools, _ = await self.snapshot()
        self.assertFalse(new_tools)
        self.assertEqual(
            len(await toolkit.get_tool_schemas()), 2
        )  # MCP + SkillViewer
        await toolkit.aclose()
        self.assertFalse(self.clients[0].is_connected)
        self.assertIs(
            self.clients[0]._connected_by, self.clients[0]._closed_by
        )

    async def test_background_call_keeps_connection_after_toolkit_release(
        self,
    ) -> None:
        """Background call keeps connection after toolkit release."""
        toolkit, tools, _ = await self.snapshot()

        async def execute() -> list[ToolChunk]:
            """Execute fixture."""
            return [chunk async for chunk in await tools[0]()]

        call = asyncio.create_task(execute())
        await self.clients[0]._test_tool.started.wait()
        record, _ = await self.records()
        await self.service.invalidate("alice", record.id)
        await toolkit.aclose()
        self.assertTrue(self.clients[0].is_connected)
        self.clients[0]._test_tool.finish.set()
        result = await call
        self.assertEqual(result[0].content[0].text, "done")
        self.assertFalse(self.clients[0].is_connected)

    async def test_runtime_scope_releases_on_error(self) -> None:
        """Runtime scope releases on error."""
        with self.assertRaisesRegex(RuntimeError, "assembly"):
            async with resource_toolkit_scope():
                toolkit, _, _ = await self.snapshot()
                record, _ = await self.records()
                await self.service.invalidate("alice", record.id)
                raise RuntimeError("assembly")
        self.assertFalse(self.clients[0].is_connected)
        await toolkit.aclose()

    async def test_scopes_do_not_share_mcp_clients(self) -> None:
        """Scopes do not share mcp clients."""
        await self.snapshot()
        record, _ = await self.records()
        await self.service.equip_mcp(
            "alice", self.workspace, "other", "session", record
        )
        await self.service.equip_mcp(
            "alice", self.workspace, "agent", "other", record
        )
        await self.snapshot(agent="other")
        await self.snapshot(session="other")
        self.assertEqual(len(self.clients), 3)

    async def test_remove_and_toggle_do_not_reseed_a_scope(self) -> None:
        """Remove and toggle do not reseed a scope."""
        await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        await self.service.remove_mcp(
            "alice",
            self.workspace,
            "agent",
            "session",
            (await self.records())[0].id,
        )
        record, _ = await self.records()
        for enabled in (False, True):
            await self.set_enabled(record, enabled)
            await self.storage.upsert_mcp("alice", record)
        listing = await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(listing, [])
        reopened = LocalWorkspace(workdir=self.workspace.workdir)
        await reopened.initialize()
        self.addAsyncCleanup(reopened.close)
        self.assertEqual(
            await self.service.list_mcps(
                "alice", reopened, "agent", "session"
            ),
            [],
        )
        self.assertEqual(
            len(
                await self.service.list_mcps("alice", reopened, "agent", "new")
            ),
            0,
        )

    async def test_sdk_declarations_do_not_change_saved_bindings(self):
        await self.workspace.set_mcp_specs(
            [], agent_id="agent", session_id="session"
        )
        self.assertEqual(
            len(
                await self.service.list_mcps(
                    "alice", self.workspace, "agent", "session"
                )
            ),
            1,
        )

    async def test_rename_retains_binding_after_restart(self) -> None:
        """Rename retains binding after restart."""
        await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        record, _ = await self.records()
        record.client = mcp("renamed")
        await self.set_enabled(record, False)
        await self.storage.upsert_mcp("alice", record)
        reopened = LocalWorkspace(workdir=self.workspace.workdir)
        await reopened.initialize()
        self.addAsyncCleanup(reopened.close)
        listing = await self.service.list_mcps(
            "alice", reopened, "agent", "session"
        )
        self.assertEqual(listing[0]["name"], "renamed")
        self.assertEqual(listing[0]["resource_id"], record.id)
        self.assertFalse(listing[0]["enabled"])

    async def test_new_scopes_never_select_library_resources_automatically(
        self,
    ) -> None:
        """Library ownership alone does not equip a new workspace scope."""
        before = dict(self.storage.mcps), dict(self.storage.skills)
        self.assertEqual(
            await self.service.list_mcps(
                "alice", self.workspace, "fresh", "session"
            ),
            [],
        )
        self.assertEqual(
            await self.service.list_skills(
                "alice", self.workspace, "fresh", "session"
            ),
            [],
        )
        self.assertEqual((self.storage.mcps, self.storage.skills), before)

    async def test_uninstall_does_not_recreate_bound_custom_records(
        self,
    ) -> None:
        """Uninstall does not recreate bound custom records."""
        record = MCPRecord(user_id="alice", client=mcp("custom"))
        await self.storage.upsert_mcp("alice", record)
        await self.service.equip_mcp(
            "alice", self.workspace, "agent", "session", record
        )
        self.storage.mcps.pop(("alice", record.id))
        listing = await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(
            next(r for r in listing if r["resource_id"] == record.id)[
                "status"
            ],
            "unavailable",
        )
        self.assertIsNone(await self.storage.get_mcp("alice", record.id))

    async def test_skill_switch_preserves_agent_edits_and_reenable_restores(
        self,
    ) -> None:
        """Skill switch preserves agent edits and reenable restores."""
        _, _, skills = await self.snapshot()
        script = Path(skills[0].dir, "scripts/run.py")
        script.write_text("agent edits", encoding="utf-8")
        _, record = await self.records()
        await self.set_enabled(record, False)
        await self.storage.upsert_skill("alice", record)
        _, _, disabled = await self.snapshot()
        self.assertEqual(disabled, [])
        self.assertEqual(script.read_text(encoding="utf-8"), "agent edits")
        await self.set_enabled(record, True)
        await self.storage.upsert_skill("alice", record)
        _, _, restored = await self.snapshot()
        self.assertEqual(restored[0].dir, skills[0].dir)
        self.assertEqual(script.read_text(encoding="utf-8"), "agent edits")

    async def upgraded_service(self) -> Any:
        """A service restart cannot infer updates from local source files."""
        (self.skill_path / "scripts/run.py").write_text(
            "print('v2')", encoding="utf-8"
        )
        service = ResourceService(self.storage, self.store)
        self.addAsyncCleanup(service.close)
        return service

    async def publish_updates(self) -> None:
        """Explicitly save user configuration and a complete Skill package."""
        record, skill = await self.records()
        record.client = mcp(record.name, url="https://example.com/v2")
        record.version = "2"
        record.description = "Browser version two."
        package = await self.store.capture_directory(str(self.skill_path))
        skill.artifact_id, skill.markdown = (
            package.artifact_id,
            package.markdown,
        )
        skill.description, skill.version = package.description, "2"
        await self.storage.upsert_mcp("alice", record)
        await self.storage.upsert_skill("alice", skill)

    async def test_source_changes_require_an_explicit_user_update(
        self,
    ) -> None:
        """Source files and a restart cannot replace stored user resources."""
        record, skill = await self.records()
        original_artifact = skill.artifact_id
        await self.upgraded_service()
        saved, saved_skill = await self.records()
        self.assertEqual(saved.version, "1")
        self.assertEqual(saved_skill.artifact_id, original_artifact)
        await self.publish_updates()
        self.assertEqual(
            (await self.storage.get_mcp("alice", record.id)).version, "2"
        )
        self.assertNotEqual(
            (await self.storage.get_skill("alice", skill.id)).artifact_id,
            original_artifact,
        )

    async def test_user_update_refreshes_descriptions_and_preserves_switches(
        self,
    ) -> None:
        """User updates keep identity and switches."""
        record, skill = await self.records()
        await self.set_enabled(record, False)
        await self.set_enabled(skill, False)
        await self.storage.upsert_mcp("alice", record)
        await self.storage.upsert_skill("alice", skill)
        markdown = self.skill_path / "SKILL.md"
        markdown.write_text(
            markdown.read_text("utf-8").replace(
                "Analyse data.", "Updated Skill instructions."
            ),
            encoding="utf-8",
        )
        await self.upgraded_service()
        await self.publish_updates()
        updated, updated_skill = await self.records()
        self.assertEqual(updated.description, "Browser version two.")
        self.assertEqual(
            updated_skill.description, "Updated Skill instructions."
        )
        self.assertEqual(updated.id, record.id)
        self.assertEqual(updated_skill.id, skill.id)
        self.assertFalse(
            (
                await self.service.list_mcps(
                    "alice", self.workspace, "agent", "session"
                )
            )[0]["enabled"]
        )
        self.assertFalse(
            (
                await self.service.list_skills(
                    "alice", self.workspace, "agent", "session"
                )
            )[0]["enabled"]
        )

    async def test_skill_upgrade_applies_explicitly_and_retains_old_files(
        self,
    ) -> None:
        """Skill upgrade applies explicitly and retains old files."""
        old = (
            await self.service.list_skills(
                "alice", self.workspace, "agent", "session"
            )
        )[0]
        service = await self.upgraded_service()
        _, record = await self.records()
        await self.publish_updates()
        _, record = await self.records()
        self.assertEqual(
            Path(old["dir"], "scripts/run.py").read_text(encoding="utf-8"),
            "print('v1')",
        )
        self.assertEqual(
            (
                await service.list_skills(
                    "alice", self.workspace, "agent", "session"
                )
            )[0]["installed_version"],
            "1",
        )
        await service.equip_skill(
            "alice", self.workspace, "agent", "session", record, upgrade=True
        )
        listing = await service.list_skills(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["installed_version"], "2")
        self.assertEqual(
            Path(listing[0]["dir"], "scripts/run.py").read_text(
                encoding="utf-8"
            ),
            "print('v2')",
        )
        self.assertEqual(
            Path(old["dir"], "scripts/run.py").read_text(encoding="utf-8"),
            "print('v1')",
        )

    async def test_skill_upgrade_refuses_modified_copy(self) -> None:
        """Skill upgrade refuses modified copy."""
        old = (
            await self.service.list_skills(
                "alice", self.workspace, "agent", "session"
            )
        )[0]
        script = Path(old["dir"], "scripts/run.py")
        script.write_text("user change", encoding="utf-8")
        service = await self.upgraded_service()
        _, record = await self.records()
        await self.publish_updates()
        _, record = await self.records()
        with self.assertRaisesRegex(ValueError, "modified"):
            await service.equip_skill(
                "alice",
                self.workspace,
                "agent",
                "session",
                record,
                upgrade=True,
            )
        self.assertEqual(script.read_text(encoding="utf-8"), "user change")

    async def test_skill_unbind_preserves_files_and_survives_restart(self):
        old = (
            await self.service.list_skills(
                "alice", self.workspace, "agent", "session"
            )
        )[0]
        await self.service.change_session_resource(
            "alice",
            "agent",
            "session",
            "skill",
            old["resource_id"],
            remove=True,
        )
        reopened = LocalWorkspace(workdir=self.workspace.workdir)
        await reopened.initialize()
        self.addAsyncCleanup(reopened.close)
        self.assertEqual(
            await self.service.list_skills(
                "alice", reopened, "agent", "session"
            ),
            [],
        )
        self.assertTrue(Path(old["dir"]).exists())

    async def test_required_environment_is_resolved_only_on_load(self) -> None:
        """Required environment is resolved only on load."""
        record, _ = await self.records()
        record.client = mcp(
            url="https://example.com/mcp?key=${ENV:RESOURCE_TEST_KEY}"
        )
        await self.storage.upsert_mcp("alice", record)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                (
                    await self.service.list_mcps(
                        "alice", self.workspace, "agent", "session"
                    )
                )[0]["status"],
                "not_loaded",
            )
            _, tools, _ = await self.snapshot()
        self.assertEqual(tools, [])
        listing = await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(listing[0]["status"], "error")
        self.assertIn("RESOURCE_TEST_KEY", listing[0]["error"])
        with patch.dict(os.environ, {"RESOURCE_TEST_KEY": "secret"}):
            await self.snapshot()
        self.assertIn("key=secret", self.clients[0].mcp_config.url)
        self.assertIn(
            "${ENV:",
            (
                await self.storage.get_mcp("alice", record.id)
            ).client.mcp_config.url,
        )

    async def test_saved_skill_is_installed_without_hub(self) -> None:
        """A package saved at registration installs offline in every scope."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "offline/SKILL.md",
                "---\nname: offline\ndescription: Offline package.\n---\n",
            )
            archive.writestr("offline/scripts/run.py", "offline script")

        async def stream() -> AsyncIterator[bytes]:
            """Supply the explicitly installed package."""
            yield buffer.getvalue()

        package = await self.store.capture_archive(stream(), "zip", "offline")
        record = SkillRecord(
            user_id="alice",
            name="offline",
            hub_id="hub",
            card_id="card",
            version="fixed",
            artifact_id=package.artifact_id,
        )
        await self.storage.upsert_skill("alice", record)
        await self.service.equip_skill(
            "alice", self.workspace, "agent", "session", record
        )
        saved = await self.storage.get_skill("alice", record.id)
        await self.service.equip_skill(
            "alice", self.workspace, "other", "session", saved
        )
        self.assertEqual(saved.artifact_id, package.artifact_id)
        skills = await self.service.list_skills(
            "alice", self.workspace, "other", "session"
        )
        installed = next(s for s in skills if s["resource_id"] == record.id)
        self.assertEqual(
            Path(installed["dir"], "scripts/run.py").read_text(
                encoding="utf-8"
            ),
            "offline script",
        )

    async def test_bad_package_does_not_publish_partial_artifact(self) -> None:
        """Bad package does not publish partial artifact."""
        (self.skill_path / "SKILL.md").write_text("invalid", encoding="utf-8")
        before = set(Path(self.store.root_dir).iterdir())
        with self.assertRaises(ValueError):
            await self.store.capture_directory(str(self.skill_path))
        self.assertEqual(set(Path(self.store.root_dir).iterdir()), before)

    async def test_reads_ignore_old_builtin_markers_without_migration(
        self,
    ) -> None:
        """Old markers impose no policy; reads leave stored data unchanged."""
        record, skill = await self.records()
        self.storage.mcps["alice", record.id]["builtin_id"] = "old-browser"
        self.storage.skills["alice", skill.id]["builtin_id"] = "old-analysis"
        before = json.dumps(
            [
                self.storage.mcps["alice", record.id],
                self.storage.skills["alice", skill.id],
            ]
        )
        mcps = await self.service.list_mcps(
            "alice", self.workspace, "agent", "session"
        )
        skills = await self.service.list_skills(
            "alice", self.workspace, "agent", "session"
        )
        self.assertEqual(mcps[0]["source"], "custom")
        self.assertEqual(skills[0]["source"], "custom")
        self.assertEqual(
            json.dumps(
                [
                    self.storage.mcps["alice", record.id],
                    self.storage.skills["alice", skill.id],
                ]
            ),
            before,
        )

    async def test_user_stdio_config_is_saved_without_catalog_constraints(
        self,
    ) -> None:
        """Selecting user configuration does not connect."""
        record = MCPRecord(
            user_id="alice",
            client=MCPClient(
                name="manual",
                is_stateful=True,
                mcp_config=StdioMCPConfig(
                    command="npx", args=["@playwright/mcp@latest"]
                ),
            ),
        )
        await self.storage.upsert_mcp("alice", record)
        await self.service.equip_mcp(
            "alice", self.workspace, "manual", "session", record
        )
        listing = await self.service.list_mcps(
            "alice", self.workspace, "manual", "session"
        )
        self.assertEqual(listing[0]["source"], "custom")
        self.assertEqual(self.clients, [])

    async def test_real_stdio_transport_owns_connect_and_close_in_one_task(
        self,
    ) -> None:
        """Real stdio transport owns connect and close in one task."""
        record, skill = await self.records()
        await self.set_enabled(skill, False)
        await self.storage.upsert_skill("alice", skill)
        record.client = MCPClient(
            name="echo",
            is_stateful=True,
            mcp_config=StdioMCPConfig(
                command=sys.executable,
                args=[
                    str(
                        Path(__file__).parent
                        / "fixtures"
                        / "resource_mcp_server.py"
                    )
                ],
            ),
        )
        await self.storage.upsert_mcp("alice", record)
        del self.workspace.create_mcp_client
        toolkit, tools, _ = await self.snapshot()
        self.assertEqual(
            len(tools),
            1,
            await self.service.list_mcps(
                "alice", self.workspace, "agent", "session"
            ),
        )
        chunks = [chunk async for chunk in await tools[0](text="hello")]
        self.assertEqual(chunks[0].content[0].text, "hello")
        await self.service.invalidate("alice", record.id)
        await toolkit.aclose()

    async def test_cancelled_connection_initialization_releases_owner(
        self,
    ) -> None:
        """Cancelled connection initialization releases owner."""
        started = asyncio.Event()

        class SlowMCP(FakeMCP):
            """Hold initialization open until it is cancelled."""

            async def connect(self) -> None:
                """Initialize the controlled fixture transport."""
                started.set()
                await asyncio.Event().wait()

        entry = _MCPEntry(
            SlowMCP.model_validate(mcp().model_dump()), "alice", "slow", "hash"
        )
        task = asyncio.create_task(entry.start())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(entry.owner.done())

    async def test_connection_owner_cancellation_reports_loading_error(
        self,
    ) -> None:
        """An unexpectedly cancelled owner must not cancel its borrower."""

        class InterruptedMCP(FakeMCP):
            """Emulate cancellation inside a failed transport task group."""

            async def connect(self) -> None:
                raise asyncio.CancelledError()

        entry = _MCPEntry(
            InterruptedMCP.model_validate(mcp().model_dump()),
            "alice",
            "interrupted",
            "hash",
        )
        with self.assertRaisesRegex(RuntimeError, "initialization.*cancelled"):
            await entry.start()
        self.assertTrue(entry.owner.done())

    async def test_connection_initialization_timeout_releases_owner(
        self,
    ) -> None:
        """An unresponsive MCP must stop owning the connection on timeout."""

        class SlowMCP(FakeMCP):
            """Hold initialization open without using a network service."""

            async def connect(self) -> None:
                await asyncio.Event().wait()

        entry = _MCPEntry(
            SlowMCP.model_validate(mcp().model_dump()),
            "alice",
            "slow",
            "hash",
        )
        with self.assertRaises(TimeoutError):
            await entry.start(timeout=0.02)
        self.assertTrue(entry.owner.done())

    async def test_independent_loading_failure_redacts_secrets(self) -> None:
        """Independent loading failure redacts secrets."""
        bad = MCPRecord(
            user_id="alice",
            client=MCPClient(
                name="bad",
                is_stateful=False,
                mcp_config=HttpMCPConfig(
                    url="https://bad.invalid",
                    headers={"Authorization": "Bearer secret"},
                ),
            ),
        )
        await self.storage.upsert_mcp("alice", bad)
        await self.service.equip_mcp(
            "alice", self.workspace, "agent", "session", bad
        )
        factory = self.workspace.create_mcp_client

        class BrokenMCP(FakeMCP):
            """Fail one transport without affecting neighboring resources."""

            async def connect(self) -> None:
                """Initialize the controlled fixture transport."""
                raise ExceptionGroup(
                    "TaskGroup",
                    [RuntimeError("secret at https://bad.invalid")],
                )

        def client(spec: Any, **kwargs: Any) -> FakeMCP:
            """Client fixture."""
            if spec.name == "bad":
                return BrokenMCP.model_validate(spec.model_dump())
            return factory(spec, **kwargs)

        self.workspace.create_mcp_client = client
        _, tools, _ = await self.snapshot()
        self.assertEqual(len(tools), 1)
        status = {
            r["name"]: r
            for r in await self.service.list_mcps(
                "alice", self.workspace, "agent", "session"
            )
        }
        self.assertEqual(status["echo"]["status"], "ready")
        self.assertEqual(status["bad"]["status"], "error")
        self.assertNotIn("secret", status["bad"]["error"])
        self.assertNotIn("TaskGroup", status["bad"]["error"])

    async def test_parallel_scope_reads_do_not_create_bindings(self):
        before = (
            await self.service._load_bindings(self.workspace, "alice")
        ).model_dump()
        await asyncio.gather(
            self.service.list_mcps("alice", self.workspace, "a", "one"),
            self.service.list_mcps("alice", self.workspace, "b", "two"),
            self.service.list_skills("alice", self.workspace, "a", "one"),
            self.service.list_skills("alice", self.workspace, "b", "two"),
        )
        self.assertEqual(
            (
                await self.service._load_bindings(self.workspace, "alice")
            ).model_dump(),
            before,
        )

    async def test_reopened_workspace_rebuilds_runtime_connections(
        self,
    ) -> None:
        """Reopened workspace rebuilds runtime connections."""
        toolkit, _, _ = await self.snapshot()
        await toolkit.aclose()
        reopened = LocalWorkspace(
            workspace_id=self.workspace.workspace_id,
            workdir=self.workspace.workdir,
        )
        await reopened.initialize()
        self.addAsyncCleanup(reopened.close)
        reopened.create_mcp_client = self.workspace.create_mcp_client
        tools, skills, entries = await self.service.snapshot(
            "alice",
            reopened,
            "agent",
            "session",
        )
        replacement = ResourceToolkit(
            entries=entries,
            tools=tools,
            skills_or_loaders=skills,
        )
        self.addAsyncCleanup(replacement.aclose)
        self.assertEqual(len(self.clients), 2)
        self.assertFalse(self.clients[0].is_connected)
        self.assertTrue(self.clients[1].is_connected)

    async def test_unbound_skill_copy_is_not_adopted_or_captured(
        self,
    ) -> None:
        """Existing files remain unbound and unavailable to new runtimes."""
        await self.workspace.add_skill(
            str(self.skill_path), agent_id="unbound"
        )
        skill = (await self.workspace.list_skills(agent_id="unbound"))[0]
        script = Path(skill.dir, "scripts/run.py")
        script.write_text("existing user edit", encoding="utf-8")
        _, record = await self.records()
        before = dict(self.storage.skills)
        artifacts = set((self.root / "artifacts").iterdir())
        self.assertEqual(
            await self.service.list_skills(
                "alice", self.workspace, "unbound", "session"
            ),
            [],
        )
        _, _, available = await self.snapshot(agent="unbound")
        self.assertEqual(available, [])
        state = await self.service._load_bindings(self.workspace, "alice")
        self.assertNotIn("unbound", state.skills)
        self.assertEqual(self.storage.skills, before)
        self.assertEqual(set((self.root / "artifacts").iterdir()), artifacts)
        self.assertEqual(
            script.read_text(encoding="utf-8"), "existing user edit"
        )
        # An explicit install creates a bound saved copy without replacing it.
        await self.service.equip_skill(
            "alice", self.workspace, "unbound", "session", record
        )
        selected = await self.service.list_skills(
            "alice", self.workspace, "unbound", "session"
        )
        self.assertEqual(len(selected), 1)
        self.assertNotEqual(selected[0]["dir"], skill.dir)
        self.assertEqual(script.read_text("utf-8"), "existing user edit")

    async def test_explicit_user_install_does_not_adopt_sdk_seed(
        self,
    ) -> None:
        """Bind the explicit saved package instead of an edited SDK seed."""
        legacy = LocalWorkspace(
            workdir=str(self.root / "legacy"),
            skill_paths=[str(self.skill_path)],
        )
        await legacy.initialize()
        self.addAsyncCleanup(legacy.close)
        seed_script = Path(
            legacy._skill_seed_dir, "analysis", "scripts/run.py"
        )
        # SDK seed directories use the source directory basename.
        self.assertTrue(seed_script.is_file())
        seed_script.write_text("old seeded version", encoding="utf-8")
        self.assertEqual(
            await self.service.list_skills(
                "alice", legacy, "new-agent", "session"
            ),
            [],
        )
        _, record = await self.records()
        await self.service.equip_skill(
            "alice", legacy, "new-agent", "session", record
        )
        selected = await self.service.list_skills(
            "alice", legacy, "new-agent", "session"
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["installed_version"], "1")
        self.assertEqual(
            Path(selected[0]["dir"], "scripts/run.py").read_text(
                encoding="utf-8"
            ),
            "print('v1')",
        )
        _, record = await self.records()
        self.assertEqual(selected[0]["resource_id"], record.id)
        self.assertEqual(seed_script.read_text("utf-8"), "old seeded version")

    async def test_sdk_defaults_are_not_automatically_registered(
        self,
    ) -> None:
        """Workspace defaults do not create user records or selections."""
        service = ResourceService(self.storage, skill_store=self.store)
        self.addAsyncCleanup(service.close)
        legacy = LocalWorkspace(
            workdir=str(self.root / "unequipped"),
            default_mcps=[mcp()],
            skill_paths=[str(self.skill_path)],
        )
        await legacy.initialize()
        self.addAsyncCleanup(legacy.close)
        self.assertEqual(
            await service.list_mcps("bob", legacy, "agent", "session"),
            [],
        )
        self.assertEqual(
            await service.list_skills("bob", legacy, "agent", "session"), []
        )
        self.assertEqual(await self.storage.list_mcps("bob"), [])
        self.assertEqual(await self.storage.list_skills("bob"), [])

    async def test_unbound_mcp_is_not_matched_or_registered(
        self,
    ) -> None:
        """An existing declaration never binds by name or config similarity."""
        await self.service.remove_mcp(
            "alice",
            self.workspace,
            "agent",
            "session",
            (await self.records())[0].id,
        )
        record, _ = await self.records()
        record.client = mcp(name="renamed")
        await self.set_enabled(record, False)
        await self.storage.upsert_mcp("alice", record)
        await self.workspace.set_mcp_specs(
            [mcp()], agent_id="agent", session_id="session"
        )
        before = dict(self.storage.mcps)
        self.assertEqual(
            await self.service.list_mcps(
                "alice", self.workspace, "agent", "session"
            ),
            [],
        )
        _, tools, _ = await self.snapshot()
        self.assertEqual(tools, [])
        self.assertEqual(self.clients, [])
        self.assertEqual(self.storage.mcps, before)

    async def test_library_reads_preserve_same_name_owned_records(
        self,
    ) -> None:
        """Listing another user's resources never modifies their library."""
        existing_mcp = MCPRecord(user_id="bob", client=mcp())
        existing_skill = SkillRecord(user_id="bob", name="analysis")
        await self.storage.upsert_mcp("bob", existing_mcp)
        await self.storage.upsert_skill("bob", existing_skill)
        before = dict(self.storage.mcps), dict(self.storage.skills)
        await self.service.list_mcps("bob", self.workspace, "new", "session")
        await self.service.list_skills("bob", self.workspace, "new", "session")
        self.assertEqual((self.storage.mcps, self.storage.skills), before)

    async def test_metadata_only_skill_is_not_backfilled(self) -> None:
        """Reads and selection require a saved package, without backfilling."""
        record = SkillRecord(
            user_id="alice",
            name="offline",
            hub_id="hub",
            card_id="card",
            version="fixed",
        )
        await self.storage.upsert_skill("alice", record)
        before = dict(self.storage.skills)
        with self.assertRaisesRegex(Exception, "no saved package"):
            await self.service.equip_skill(
                "alice", self.workspace, "agent", "session", record
            )
        self.assertEqual(self.storage.skills, before)
        source = self.root / "offline"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: offline\ndescription: Package.\n---\n", "utf-8"
        )
        package = await self.store.capture_directory(str(source))
        with self.assertRaisesRegex(ValueError, "Update its saved package"):
            await self.service.register_skill_package("alice", package)
        self.assertEqual(self.storage.skills, before)

    async def test_concurrent_identical_artifact_capture_is_idempotent(
        self,
    ) -> None:
        """Two imports publishing the same new package share one artifact."""
        (self.skill_path / "scripts/run.py").write_text(
            "a previously unsaved revision", encoding="utf-8"
        )
        barrier = threading.Barrier(2, timeout=5)
        attempts = threading.local()
        replace = os.replace

        def publish(source: str, destination: str) -> None:
            if not getattr(attempts, "started", False):
                attempts.started = True
                barrier.wait()
            replace(source, destination)

        with patch(
            "agentscope.app.resources._skill_store.os.replace", new=publish
        ):
            packages = await asyncio.gather(
                self.store.capture_directory(str(self.skill_path)),
                self.store.capture_directory(str(self.skill_path)),
            )
        self.assertEqual(packages[0], packages[1])
        self.assertEqual(
            Path(
                self.store.path(packages[0].artifact_id), "scripts/run.py"
            ).read_text("utf-8"),
            "a previously unsaved revision",
        )
        self.assertTrue(
            all(len(p.name) == 64 for p in Path(self.store.root_dir).iterdir())
        )

    async def test_artifact_publish_retries_transient_windows_lock(
        self,
    ) -> None:
        """A short Windows sharing lock must not lose a valid new package."""
        (self.skill_path / "scripts/run.py").write_text("new", "utf-8")
        replace = os.replace
        count = 0

        def temporarily_locked(source: str, destination: str) -> None:
            nonlocal count
            count += 1
            if count <= 2:
                error = PermissionError("Temporary sharing lock")
                error.winerror = 32
                raise error
            replace(source, destination)

        with patch(
            "agentscope.app.resources._skill_store.os.replace",
            new=temporarily_locked,
        ):
            package = await self.store.capture_directory(str(self.skill_path))
        self.assertGreaterEqual(count, 3)
        self.assertEqual(
            Path(
                self.store.path(package.artifact_id), "scripts/run.py"
            ).read_text("utf-8"),
            "new",
        )

    async def test_artifact_publish_does_not_hide_persistent_denial(
        self,
    ) -> None:
        """Bounded retries preserve old packages and surface real failures."""
        (self.skill_path / "scripts/run.py").write_text("new", "utf-8")
        previous = set(Path(self.store.root_dir).iterdir())
        for winerror, expected_attempts in ((5, 6), (None, 1)):
            with self.subTest(winerror=winerror):
                error = PermissionError("Access denied")
                error.winerror = winerror
                with (
                    patch(
                        "agentscope.app.resources._skill_store.os.replace",
                        side_effect=error,
                    ) as replace,
                    patch("agentscope.app.resources._skill_store.time.sleep"),
                    self.assertRaises(PermissionError),
                ):
                    await self.store.capture_directory(str(self.skill_path))
                self.assertEqual(replace.call_count, expected_attempts)
                self.assertEqual(
                    set(Path(self.store.root_dir).iterdir()), previous
                )
