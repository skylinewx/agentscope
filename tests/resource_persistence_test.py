# -*- coding: utf-8 -*-
"""Atomic scope updates across independent SQL storage instances."""

import asyncio
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from agentscope.app.storage import (
    AsyncSQLAlchemyStorage,
    AgentData,
    AgentRecord,
    SessionConfig,
    ResourceBinding,
    ResourceOverride,
    SkillResourceBinding,
)
from agentscope.state import AgentState


class ResourcePersistenceTest(IsolatedAsyncioTestCase):
    async def test_atomic_resource_state_and_name_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = "sqlite+aiosqlite:///" + str(Path(tmp, "scopes.db"))
            async with (
                AsyncSQLAlchemyStorage(url, create_tables=True) as first,
                AsyncSQLAlchemyStorage(url) as second,
            ):
                agent = AgentRecord(
                    user_id="alice", data=AgentData(name="agent")
                )
                agent.data.resource_config.mcps["mcp"] = ResourceBinding(
                    enabled=False
                )
                agent.data.resource_config.skills["skill"] = (
                    SkillResourceBinding(artifact_id="a" * 64, version="1")
                )
                await first.upsert_agent("alice", agent)
                session = await first.upsert_session(
                    "alice", agent.id, SessionConfig(workspace_id="shared")
                )

                def change_mcp(record):
                    record.config.resource_config.mcp_overrides["mcp"] = (
                        ResourceOverride(
                            enabled=True,
                            binding_id=agent.data.resource_config.mcps[
                                "mcp"
                            ].binding_id,
                        )
                    )

                def change_skill(record):
                    record.config.resource_config.skills["local"] = (
                        SkillResourceBinding(
                            enabled=False, artifact_id="b" * 64
                        )
                    )

                def rename(record):
                    record.config.name = "renamed"

                await asyncio.gather(
                    first.mutate_session(
                        "alice", agent.id, session.id, change_mcp
                    ),
                    second.mutate_session(
                        "alice", agent.id, session.id, change_skill
                    ),
                    first.mutate_session(
                        "alice", agent.id, session.id, rename
                    ),
                    second.update_session_state(
                        "alice", agent.id, session.id, AgentState()
                    ),
                )
                saved = await second.get_session("alice", agent.id, session.id)
                self.assertEqual(saved.config.name, "renamed")
                self.assertTrue(
                    saved.config.resource_config.mcp_overrides["mcp"].enabled
                )
                self.assertFalse(
                    saved.config.resource_config.skills["local"].enabled
                )
                self.assertEqual(
                    (
                        await second.get_agent("alice", agent.id)
                    ).data.resource_config,
                    agent.data.resource_config,
                )
                with self.assertRaises(KeyError):
                    await second.mutate_session(
                        "bob", agent.id, session.id, rename
                    )

                def agent_mcp(record):
                    record.data.resource_config.mcps["mcp"].enabled = True

                def agent_skill(record):
                    record.data.resource_config.skills["skill"].enabled = False

                await asyncio.gather(
                    first.mutate_agent("alice", agent.id, agent_mcp),
                    second.mutate_agent("alice", agent.id, agent_skill),
                )
                saved_agent = await first.get_agent("alice", agent.id)
                self.assertTrue(
                    saved_agent.data.resource_config.mcps["mcp"].enabled
                )
                self.assertFalse(
                    saved_agent.data.resource_config.skills["skill"].enabled
                )
