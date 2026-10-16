"""Scope semantics independently of transport, storage and UI."""

import unittest

from agentscope.app.storage import (
    AgentResourceConfig,
    ResourceBinding,
    ResourceOverride,
    SessionResourceConfig,
    SkillResourceBinding,
)
from agentscope.app._service._resource_resolution import resolve_resources


class ResourceResolutionTest(unittest.TestCase):
    def test_switch_truth_table_for_both_kinds(self):
        for kind in ("mcp", "skill"):
            for default in (False, True):
                for override in (None, False, True):
                    with self.subTest(
                        kind=kind, default=default, override=override
                    ):
                        binding = (
                            ResourceBinding
                            if kind == "mcp"
                            else SkillResourceBinding
                        )(enabled=default)
                        agent = AgentResourceConfig(
                            **{kind + "s": {"r": binding}}
                        )
                        session = SessionResourceConfig()
                        if override is not None:
                            getattr(session, kind + "_overrides")["r"] = (
                                ResourceOverride(
                                    enabled=override,
                                    binding_id=binding.binding_id,
                                )
                            )
                        actual = resolve_resources(agent, session)[0]
                        self.assertEqual(
                            actual.enabled,
                            default if override is None else override,
                        )
                        self.assertEqual(actual.session_override, override)

    def test_dynamic_inheritance_and_independent_addition(self):
        agent = AgentResourceConfig(mcps={"r": ResourceBinding()})
        local = SessionResourceConfig(mcps={"extra": ResourceBinding()})
        self.assertEqual(len(resolve_resources(agent, local)), 2)
        self.assertEqual(len(resolve_resources(agent)), 1)
        agent.mcps["r"].enabled = False
        self.assertFalse(resolve_resources(agent, local)[0].enabled)
        self.assertTrue(resolve_resources(agent, local)[1].enabled)

    def test_unbind_rebind_does_not_revive_override(self):
        binding = ResourceBinding()
        session = SessionResourceConfig(
            mcp_overrides={
                "r": ResourceOverride(
                    enabled=False,
                    binding_id=binding.binding_id,
                )
            }
        )
        self.assertEqual(resolve_resources(AgentResourceConfig(), session), [])
        rebound = AgentResourceConfig(mcps={"r": ResourceBinding()})
        self.assertTrue(resolve_resources(rebound, session)[0].enabled)

    def test_local_selection_deduplicates_and_survives_agent_unbind(self):
        agent = AgentResourceConfig(mcps={"r": ResourceBinding(enabled=False)})
        local = SessionResourceConfig(mcps={"r": ResourceBinding()})
        effective = resolve_resources(agent, local)
        self.assertEqual(len(effective), 1)
        self.assertTrue(effective[0].enabled)
        self.assertEqual(effective[0].binding_sources, ["agent", "session"])
        agent.mcps.clear()
        self.assertTrue(resolve_resources(agent, local)[0].enabled)

    def test_restore_inheritance_and_explicit_equal_value(self):
        binding = ResourceBinding()
        agent = AgentResourceConfig(mcps={"r": binding})
        session = SessionResourceConfig(
            mcp_overrides={
                "r": ResourceOverride(
                    enabled=True,
                    binding_id=binding.binding_id,
                )
            }
        )
        agent.mcps["r"].enabled = False
        self.assertTrue(resolve_resources(agent, session)[0].enabled)
        session.mcp_overrides.clear()
        self.assertFalse(resolve_resources(agent, session)[0].enabled)
