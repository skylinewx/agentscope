import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agentscope.message import UserMsg
from agentscope.state import AgentState
from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.analysis_requirements import assess, REQUIREMENT_SCHEMA
from analysis_extension.followup_scope import _inherit_business_request
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion, current_turn, review_payload


class RequirementTest(PresentationCase):
    def turn(self, question='从利润表分析一下亏损企业'):
        state = current_turn(AgentState(), UserMsg('user', question))
        state['previousMessages'] = [
            {'role': 'user', 'text': '查询一下久其集团26年的净利润预算'},
            {'role': 'user', 'text': '查询一下二级单位，从大到小排'},
            {'role': 'assistant', 'text': '不可信的旧助手分析目标'},
        ]
        self.service.turn = TurnCompletion(self.service, state)
        return self.service.turn

    def resolver(self, evidence='FORMULAS'):
        self.service.requirement_resolver = AsyncMock(return_value={
            'requiredEvidence': evidence, 'explanation': '依据用户原话确认本题必要范围。'})
        return self.service.requirement_resolver

    async def declare(self, **extra):
        return await self.service.task(action='declare', mode='ANALYSIS',
            goal='只计算亏损数量、金额和占比', requiredEvidence='VALUES', **extra)

    async def test_original_request_upgrades_narrow_plan_and_routes_filtered_formula_query(self):
        turn = self.turn()
        resolver = self.resolver()
        predicate = {'operator': 'LT', 'value': '0'}
        result = await self.declare()
        self.assertEqual(result['requiredEvidence'], 'FORMULAS')
        self.assertEqual(turn.state['requiredEvidence'], 'FORMULAS')
        payload = resolver.await_args.args[0]
        self.assertEqual(payload['userRequest'], '从利润表分析一下亏损企业')
        self.assertNotIn('只计算', json.dumps(payload, ensure_ascii=False))
        self.assertNotIn('不可信', json.dumps(payload, ensure_ascii=False))
        submitted = {'purpose': 'QUERY', 'intentDraft': {'valueFilter': predicate}}
        effective = _inherit_business_request(turn, {}, submitted)
        self.assertEqual(effective['purpose'], 'QUERY')
        self.assertEqual(effective['intentDraft']['valueFilter'], predicate)
        self.assertEqual(effective['question'], turn.state['request'])
        self.assertEqual(submitted, {'purpose': 'QUERY', 'intentDraft': {'valueFilter': predicate}})

    async def test_redeclaration_reuses_assessment_and_cannot_downgrade(self):
        turn = self.turn()
        resolver = self.resolver()
        first = await self.declare()
        result = await self.service.task(mode='ANALYSIS', goal='仍然只看金额', requiredEvidence='VALUES')
        resolver.assert_awaited_once()
        self.assertEqual(result['analysisRequirement'], first['analysisRequirement'])
        self.assertEqual(turn.state['requiredEvidence'], 'FORMULAS')
        with self.assertRaises(AnalysisError):
            turn.task(mode='ANALYSIS', goal='直接降级', requiredEvidence='VALUES')

    async def test_distribution_assessment_keeps_values_query(self):
        turn = self.turn('从板块口径分析一下二级单位的数据')
        self.resolver('VALUES')
        result = await self.declare()
        self.assertEqual(result['requiredEvidence'], 'VALUES')
        self.assertEqual(_inherit_business_request(turn, {}, {'purpose': 'ANALYZE'})['purpose'], 'QUERY')

    async def test_formula_task_does_not_expand_every_unfiltered_reference_query(self):
        turn = self.turn()
        self.resolver()
        await self.declare()
        self.assertEqual(turn.state['requiredEvidence'], 'FORMULAS')
        self.assertEqual(_inherit_business_request(turn, {}, {'purpose': 'QUERY'})['purpose'], 'QUERY')

    async def test_direct_ranking_does_not_invoke_assessment(self):
        self.turn('查询二级单位净利润，从大到小排')
        resolver = self.resolver()
        result = await self.service.task(mode='DIRECT', goal='净利润排序', requiredEvidence='VALUES')
        resolver.assert_not_awaited()
        self.assertEqual(result['requiredEvidence'], 'VALUES')

    async def test_missing_evidence_flag_uses_assessed_values_requirement(self):
        turn = self.turn('统计各板块分布占比')
        self.resolver('VALUES')
        result = await self.service.task(mode='ANALYSIS', goal='分布占比')
        self.assertEqual(result['requiredEvidence'], 'VALUES')
        self.assertEqual(_inherit_business_request(turn, {}, {'purpose': 'ANALYZE'})['purpose'], 'QUERY')

    async def test_invalid_declaration_does_not_call_model_or_mutate_turn(self):
        turn = self.turn()
        resolver = self.resolver()
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError):
            await self.service.task(mode='ANALYSIS', goal='', requiredEvidence='VALUES')
        resolver.assert_not_awaited()
        self.assertEqual(turn.state, before)

    async def test_invalid_assessment_cannot_install_values_fallback(self):
        turn = self.turn()
        self.service.requirement_resolver = AsyncMock(return_value={'issues': []})
        before = deepcopy(turn.state)
        with self.assertRaises(AnalysisError) as rejected:
            await self.declare()
        self.assertEqual(rejected.exception.code, 'INVALID_ANALYSIS_REQUIREMENT')
        self.assertEqual(turn.state, before)

    async def test_model_timeout_is_reported_and_interruption_propagates(self):
        turn = self.turn()
        model = SimpleNamespace(generate_structured_output=AsyncMock(side_effect=TimeoutError()))
        agent = SimpleNamespace(model=model)
        self.service.requirement_resolver = lambda payload: assess(agent, payload)
        with self.assertRaises(AnalysisError) as rejected:
            await self.declare()
        self.assertEqual(rejected.exception.code, 'ANALYSIS_REQUIREMENT_UNAVAILABLE')
        self.assertIsNone(turn.state['mode'])
        self.assertNotIn('analysisRequirement', turn.state)
        self.assertEqual(model.generate_structured_output.await_args.kwargs['structured_model'], REQUIREMENT_SCHEMA)
        model.generate_structured_output.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.declare()

    async def test_completed_assessment_cannot_cross_user_turn(self):
        turn = self.turn()
        async def switched(payload):
            self.turn('查询板块分布')
            return {'requiredEvidence': 'FORMULAS', 'explanation': '前一轮需要构成。'}
        self.service.requirement_resolver = switched
        with self.assertRaises(AnalysisError) as rejected:
            await self.declare()
        self.assertEqual(rejected.exception.code, 'ANALYSIS_REQUIREMENT_TURN_CHANGED')
        self.assertNotIn('analysisRequirement', turn.state)
        self.assertNotIn('analysisRequirement', self.service.turn.state)

    async def test_slow_task_declaration_stays_in_live_state_instead_of_offloading(self):
        from agentscope.app.middleware._tool_offload_middleware import ToolOffloadMiddleware
        from agentscope.message import ToolCallBlock
        from agentscope.tool import ToolChunk
        from analysis_extension.tools import AnalysisTask
        turn = self.turn()
        async def slow_assessment(payload):
            await asyncio.sleep(0.02)
            return {'requiredEvidence': 'FORMULAS', 'explanation': '解释利润形成的必要构成。'}
        self.service.requirement_resolver = slow_assessment
        tool = AnalysisTask(self.service)
        manager, bus = MagicMock(), MagicMock()
        middleware = ToolOffloadMiddleware(manager, bus, 'test-user', 'test-agent', timeout_secs=0.001)
        agent = SimpleNamespace(toolkit=SimpleNamespace(get_tool=AsyncMock(return_value=tool)))
        call = ToolCallBlock(id='declaration', name='AnalysisTask', input='{}')
        async def execute(**kwargs):
            yield await tool.call(_agent_state=AgentState(), mode='ANALYSIS', goal='只做金额统计', requiredEvidence='VALUES')
        results = [result async for result in middleware.on_acting(agent, {'tool_call': call}, execute)]
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], ToolChunk)
        text = results[0].content[0].text
        self.assertTrue(text.startswith('本轮任务：'))
        navigation = json.loads(text.split('```json\n', 1)[1].split('\n```', 1)[0])
        self.assertEqual(navigation['requiredEvidence'], 'FORMULAS')
        self.assertEqual(turn.state['requiredEvidence'], 'FORMULAS')
        manager.register_task.assert_not_called()
        bus.queue_push.assert_not_called()

    async def test_user_context_selection_reassesses_without_agent_summary(self):
        turn = self.turn('继续分析之前那项结果')
        turn.state['businessContexts'] = [
            {'contextRef': ref, 'request': request, 'intent': {}, 'subjectScope': {},
             'kind': 'DELIVERED', 'status': 'DIRECT', 'goal': '不可作为用户要求的计划'}
            for ref, request in (('a', '查询净利润'), ('b', '查询营业收入'))]
        resolver = self.resolver('VALUES')
        await self.declare(context={'mode': 'CONTINUE', 'contextRefs': ['a']})
        await self.declare(context={'mode': 'CONTINUE', 'contextRefs': ['b']})
        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(resolver.await_args.args[0]['selectedContextRequests'],
                         [{'contextRef': 'b', 'request': '查询营业收入'}])

    async def test_statistics_only_artifact_cannot_pass_confirmed_composition_requirement(self):
        reference, selectors, scope = saved_fixture(self.store)
        built = await self.service.build([reference], selectors, scope, [], 'amounts-only')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        turn = self.turn()
        self.resolver()
        await self.declare()
        _, issues, _ = turn.data_issues(artifact)
        self.assertIn('FORMULA_TARGETS_REQUIRED', [issue['code'] for issue in issues])
        payload = json.loads(review_payload(turn, artifact, ''))
        self.assertEqual(payload['userRequest'], '从利润表分析一下亏损企业')
        self.assertEqual(payload['analysisRequirement']['requiredEvidence'], 'FORMULAS')
        self.assertEqual(payload['agentPlan']['goal'], '只计算亏损数量、金额和占比')
        self.assertNotIn('goal', payload)

    async def test_new_user_turn_does_not_inherit_previous_formula_requirement(self):
        turn = self.turn()
        self.resolver()
        await self.declare()
        from analysis_extension.turn_completion import STATE_KEY
        agent_state = AgentState()
        agent_state.middle_context[STATE_KEY] = turn.state
        state = current_turn(agent_state, UserMsg('user', '从板块口径分析二级单位'))
        self.assertNotIn('analysisRequirement', state)
        self.service.turn = TurnCompletion(self.service, state)
        self.resolver('VALUES')
        self.assertEqual((await self.declare())['requiredEvidence'], 'VALUES')
