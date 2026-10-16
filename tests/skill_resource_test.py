"""Stable, scoped skill-reference reads and compression recovery."""
import json
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase

from agentscope.agent._skill_recovery import preserve_references, recovery_hint
from agentscope.message import AssistantMsg, ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.skill import Skill
from agentscope.state import AgentState
from agentscope.tool._builtin._skill import SkillViewer


class SkillResourceTests(IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.install = self.root/'version-one'
        (self.install/'references').mkdir(parents=True)
        (self.install/'references/guide.md').write_bytes('计算与展示\n**完整规则**'.encode('utf-8'))
        self.skill = Skill('generic', 'description', str(self.install), '# entry', 0)
        self.skills = {'generic': self.skill}
        self.groups = []
        async def available(groups):
            self.groups.append(groups)
            return self.skills if 'active' in groups else {}
        self.viewer = SkillViewer(available)
        self.state = AgentState()
        self.state.tool_context.activated_groups.append('active')

    async def read(self, resource=None):
        return await self.viewer.call('generic', self.state, resource)

    async def test_relative_read_reloads_current_installation_and_preserves_unicode(self):
        for resource in ('references/guide.md', 'references\\guide.md'):
            self.assertEqual((await self.read(resource)).content[0].text, '计算与展示\n**完整规则**')
        next_dir = self.root/'version-two'
        (next_dir/'references').mkdir(parents=True)
        (next_dir/'references/guide.md').write_text('新版规则', 'utf-8-sig')
        self.skill.dir = str(next_dir)
        self.assertEqual((await self.read('references/guide.md')).content[0].text, '新版规则')
        self.assertEqual((await self.read()).content[0].text, '# entry')

    async def test_disabled_skill_does_not_expose_reference(self):
        self.state.tool_context.activated_groups.clear()
        result = await self.read('references/guide.md')
        self.assertEqual(result.state, ToolResultState.ERROR)
        self.assertIn('SkillNotFoundError', result.content[0].text)

    async def test_paths_cannot_escape_active_skill(self):
        (self.root/'secret.md').write_text('outside-content', 'utf-8')
        for resource in ('../secret.md', 'references/../../secret.md', '/secret.md',
                         'C:/secret.md', 'C:secret.md', '\\\\server\\secret.md',
                         'references/guide.md:stream', '\x00', '', 3):
            with self.subTest(resource=resource):
                result = await self.read(resource)
                self.assertEqual(result.state, ToolResultState.ERROR)
                self.assertNotIn('outside-content', result.content[0].text)
                self.assertIn('SkillResourceReadError', result.content[0].text)

    async def test_missing_resource_has_retry_and_never_substitutes_near_match(self):
        result = await self.read('references/guied.md')
        self.assertEqual(result.state, ToolResultState.ERROR)
        self.assertIn('FileNotFoundError', result.content[0].text)
        self.assertIn("Skill(skill='generic')", result.content[0].text)
        self.assertIn('not read', result.content[0].text)
        self.assertNotIn('完整规则', result.content[0].text)

    async def test_nontext_directory_and_oversized_files_are_rejected(self):
        for name, raw in [('binary.bin', b'\x00abc'), ('bad.txt', b'\xff'), ('large.md', b'x'*(256*1024+1))]:
            (self.install/name).write_bytes(raw)
            self.assertEqual((await self.read(name)).state, ToolResultState.ERROR)
        self.assertEqual((await self.read('references')).state, ToolResultState.ERROR)

    async def test_resolved_resource_outside_root_is_rejected(self):
        # Windows symlink creation needs privileges; mock resolution while
        # exercising the boundary check and assert the outside file is not read.
        from unittest.mock import patch
        outside = self.root/'secret.md'
        outside.write_text('outside-content', 'utf-8')
        with patch.object(Path, 'resolve', side_effect=[self.install, outside]):
            result = await self.read('references/link.md')
        self.assertEqual(result.state, ToolResultState.ERROR)
        self.assertNotIn('outside-content', result.content[0].text)

    def messages(self, resource, state=ToolResultState.SUCCESS):
        return [AssistantMsg('agent', [
            ToolCallBlock(id='r', name='Skill', input=json.dumps({'skill':'generic', 'resource':resource})),
            ToolResultBlock(id='r', name='Skill', output='contents', state=state)])]

    async def test_compression_tracks_reference_instead_of_entry_and_restores_relative_call(self):
        messages = self.messages('references/guide.md')
        refs = preserve_references([], messages, self.skills)
        self.assertTrue(refs[0]['file_path'].endswith('/references/guide.md'))
        self.assertNotIn('SKILL.md', refs[0]['file_path'])
        self.assertIn('"resource": "references/guide.md"', recovery_hint(refs, [], self.skills))
        self.assertIsNone(recovery_hint(refs, messages, self.skills))
        self.assertEqual(preserve_references([], self.messages('references/guide.md', ToolResultState.ERROR), self.skills), [])
        self.assertEqual(preserve_references([], self.messages('../secret.md'), self.skills), [])

    async def test_full_relative_read_satisfies_prior_partial_read(self):
        refs = [{'skill':'generic', 'directory':self.install.as_posix(),
                 'file_path':(self.install/'references/guide.md').as_posix(), 'offset':10, 'limit':20}]
        self.assertIsNone(recovery_hint(refs, self.messages('references/guide.md'), self.skills))
        self.skills['generic'].dir = str(self.root/'replacement')
        self.assertIsNone(recovery_hint(refs, [], self.skills))

    async def test_toolkit_schema_dispatches_relative_resource_without_model(self):
        from agentscope.tool import Toolkit, ToolResponse
        from jsonschema import Draft202012Validator
        toolkit = Toolkit(skills_or_loaders=[self.skill])
        schema = next(s['function']['parameters'] for s in await toolkit.get_tool_schemas()
                      if s['function']['name'] == 'Skill')
        args = {'skill':'generic', 'resource':'references/guide.md'}
        Draft202012Validator(schema).validate(args)
        outputs = [r async for r in toolkit.call_tool(
            ToolCallBlock(id='read', name='Skill', input=json.dumps(args)), self.state)]
        response = next(r for r in outputs if isinstance(r, ToolResponse))
        self.assertEqual(response.state, ToolResultState.SUCCESS)
        self.assertIn('完整规则', response.content[0].text)

    async def test_agent_compression_requests_the_actual_resource_again(self):
        from unittest.mock import AsyncMock
        from agentscope.agent import Agent, ContextConfig, InjectionConfig
        from agentscope.model import StructuredResponse
        from utils import MockModel
        model = MockModel(context_size=1000)
        model.count_tokens = AsyncMock(return_value=900)
        model.set_structured_response(StructuredResponse(content={k:'summary' for k in (
            'task_overview','current_state','important_discoveries','next_steps','context_to_preserve')}))
        agent = Agent('test', system_prompt='test', model=model,
                      injection_config=InjectionConfig(inject_runtime_state=False))
        agent.toolkit._get_available_skills = AsyncMock(return_value=self.skills)
        agent.state.context = self.messages('references/guide.md')
        agent._split_context_for_compression = AsyncMock(return_value=(agent.state.context, []))
        await agent.compress_context(ContextConfig(trigger_ratio=0.7, reserve_ratio=0.1))
        self.assertTrue(agent.state.skill_read_refs[0]['file_path'].endswith('/references/guide.md'))
        prepared = await agent._prepare_model_input()
        text = '\n'.join(str(m.model_dump()) for m in prepared['messages'])
        self.assertIn('"resource": "references/guide.md"', text)
