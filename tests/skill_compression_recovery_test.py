import json
from unittest import TestCase, IsolatedAsyncioTestCase
from agentscope.agent._skill_recovery import preserve_references, recovery_hint
from agentscope.message import AssistantMsg, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.skill import Skill
from agentscope.state import AgentState


class SkillCompressionRecoveryTests(TestCase):
    def setUp(self):
        self.skills = {'generic': Skill('generic', '', '/skills/generic', 'Instructions', 0)}

    def read(self, path, state=ToolResultState.SUCCESS, **kwargs):
        return [AssistantMsg('agent', [ToolCallBlock(id='r', name='Read', input=json.dumps({'file_path':path, **kwargs})),
                                      ToolResultBlock(id='r', name='Read', output='contents', state=state)])]

    def test_restore_references_only_for_successful_registered_skill_reads(self):
        messages = self.read('/skills/generic/references/visual.md', offset=10, limit=20)
        refs = preserve_references([], messages, self.skills)
        self.assertEqual(refs[0]['limit'], 20)
        self.assertIn('re-read', recovery_hint(refs, [], self.skills))
        self.assertIsNone(recovery_hint(refs, messages, self.skills))
        restored = AgentState.model_validate(AgentState(skill_read_refs=refs).model_dump())
        self.assertEqual(restored.skill_read_refs, refs)

    def test_replaced_disabled_failed_and_unrelated_documents_are_not_restored(self):
        refs = preserve_references([], self.read('/skills/generic/references/visual.md'), self.skills)
        self.assertIsNone(recovery_hint(refs, [], {}))
        replaced = {'generic': Skill('generic', '', '/skills/new-install', 'new', 1)}
        self.assertIsNone(recovery_hint(refs, [], replaced))
        for path in ('/other/instructions.md','/skills/generic/../untrusted.md'):
            self.assertEqual(preserve_references([], self.read(path), self.skills), [])
        self.assertEqual(preserve_references([], self.read('/skills/generic/SKILL.md', ToolResultState.ERROR), self.skills), [])

    def test_distinct_read_ranges_are_preserved_and_restored_independently(self):
        first=self.read('/skills/generic/references/visual.md',offset=1,limit=50)
        second=self.read('/skills/generic/references/visual.md',offset=51,limit=50)
        refs=preserve_references([],first+second,self.skills)
        self.assertEqual(len(refs),2)
        hint=recovery_hint(refs,second,self.skills)
        self.assertIn('"offset": 1,',hint)
        self.assertNotIn('"offset": 51,',hint)


class SkillRecoveryIntegrationTests(IsolatedAsyncioTestCase):
    async def test_compression_persists_reference_and_next_model_input_requests_reread(self):
        from unittest.mock import AsyncMock
        from agentscope.agent import Agent, ContextConfig, InjectionConfig
        from agentscope.model import StructuredResponse
        from utils import MockModel
        model=MockModel(context_size=1000)
        model.count_tokens=AsyncMock(return_value=900)
        model.set_structured_response(StructuredResponse(content={k:'summary' for k in ('task_overview','current_state','important_discoveries','next_steps','context_to_preserve')}))
        agent=Agent('test',system_prompt='test',model=model,injection_config=InjectionConfig(inject_runtime_state=False))
        skills={'generic':Skill('generic','','/skills/generic','instructions',0)}
        agent.toolkit._get_available_skills=AsyncMock(return_value=skills)
        agent.state.context=[AssistantMsg('test',[
            ToolCallBlock(id='read',name='Read',input=json.dumps({'file_path':'/skills/generic/references/display.md'})),
            ToolResultBlock(id='read',name='Read',output='instructions',state=ToolResultState.SUCCESS)])]
        agent._split_context_for_compression=AsyncMock(return_value=(agent.state.context,[]))
        await agent.compress_context(ContextConfig(trigger_ratio=0.7,reserve_ratio=0.1))
        self.assertEqual(len(agent.state.skill_read_refs),1)
        self.assertEqual(agent.state.context,[])
        prepared=await agent._prepare_model_input()
        self.assertIn('skill-recovery',json.dumps([m.model_dump() for m in prepared['messages']]))
        agent.toolkit._get_available_skills=AsyncMock(return_value={})
        prepared=await agent._prepare_model_input()
        self.assertNotIn('skill-recovery',json.dumps([m.model_dump() for m in prepared['messages']]))
