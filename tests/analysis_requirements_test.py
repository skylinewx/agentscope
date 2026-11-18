from unittest.mock import AsyncMock
from agentscope.message import UserMsg
from agentscope.state import AgentState
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError
from analysis_extension.turn_completion import TurnCompletion, current_turn


class RequirementTest(PresentationCase):
    def turn(self, question='分析所选结果'):
        self.service.turn = TurnCompletion(self.service, current_turn(AgentState(), UserMsg('user', question)))
        return self.service.turn

    async def declare(self, **extra):
        return await self.service.task(mode='ANALYSIS', goal='分析所选结果', **extra)

    async def test_query_material_needs_do_not_trigger_another_model_or_force_formula(self):
        turn = self.turn()
        self.service.requirement_resolver = AsyncMock(side_effect=RuntimeError('must not call'))
        result = await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}])
        self.service.requirement_resolver.assert_not_awaited()
        self.assertEqual(result['materials'], [{'role':'PRIMARY', 'kinds':['VALUES']}])
        self.assertEqual(turn.state['analysisRequirement']['origin'], 'MODEL_PLAN')

    async def test_plan_can_change_without_erasing_actual_user_requirement(self):
        turn = self.turn('解释净利润公式构成')
        await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES', 'FORMULAS']}])
        await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}])
        self.assertEqual(turn.state['materials'], [{'role':'PRIMARY', 'kinds':['VALUES']}])
        self.assertEqual(turn.state['analysisRequirement']['basis']['userRequest'], '解释净利润公式构成')
        self.assertEqual(turn.state['request'], '解释净利润公式构成')

    async def test_explicit_investigation_has_real_quote_and_survives_same_turn(self):
        turn = self.turn('查明公式复算差额的原因')
        result = await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES', 'FORMULAS']}], investigations=[{
            'requirement':'调查差额原因', 'userQuote':'查明公式复算差额的原因', 'scope':'所选公式'}])
        self.assertTrue(result['analysisRequirement']['investigations'][0]['requirementRef'])
        await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}])
        self.assertEqual(len(turn.state['analysisRequirement']['investigations']), 1)
        new = self.turn('只列金额')
        await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}])
        self.assertEqual(new.state['analysisRequirement']['investigations'], [])

    async def test_invented_investigation_quote_does_not_mutate_task(self):
        turn = self.turn('分析亏损企业')
        with self.assertRaises(AnalysisError):
            await self.declare(investigations=[{'requirement':'追加调查', 'userQuote':'调查未知原因', 'scope':'所有单位'}])
        self.assertIsNone(turn.state['mode'])
        self.assertNotIn('analysisRequirement', turn.state)

    async def test_invalid_declaration_leaves_task_unchanged(self):
        turn = self.turn()
        with self.assertRaises(AnalysisError):
            await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['UNKNOWN']}])
        self.assertIsNone(turn.state['mode'])
        self.assertNotIn('analysisRequirement', turn.state)

    async def test_new_turn_does_not_inherit_formula_material_plan(self):
        self.turn('解释公式')
        await self.declare(materials=[{'role': 'PRIMARY', 'kinds': ['VALUES', 'FORMULAS']}])
        turn = self.turn('分析已取得分组的分布')
        result = await self.declare()
        self.assertEqual(result['materials'], [{'role':'PRIMARY', 'kinds':['VALUES']}])
        self.assertEqual(turn.state['analysisRequirement']['materials'], [{'role':'PRIMARY', 'kinds':['VALUES']}])

    async def test_direct_query_accepts_empty_investigations_without_model_call(self):
        turn = self.turn('查询金额')
        result = await self.service.task(mode='DIRECT', goal='查询金额', investigations=[])
        self.assertEqual(result['mode'], 'DIRECT')
        self.assertEqual(turn.state['analysisRequirement']['investigations'], [])

    async def test_review_only_blocks_queries_computations_and_self_relaxation(self):
        from analysis_extension.followup_scope import prepare_request
        turn = self.turn('继续审查，不重新取数或计算')
        declared = await self.declare(executionPolicy={'sourceQueries': False, 'computations': False})
        self.assertEqual(declared['executionPolicy'], {'sourceQueries': False, 'computations': False})
        with self.assertRaises(AnalysisError) as caught:
            prepare_request(turn, {'originalToolName':'execute_budget_query'}, {'purpose':'QUERY'})
        self.assertEqual(caught.exception.code, 'EXECUTION_POLICY_BLOCKED')
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(code='result=[]', purpose='计算', requestKey='blocked')
        self.assertEqual(caught.exception.code, 'EXECUTION_POLICY_BLOCKED')
        from analysis_extension.value_references import reference_context
        from numeric_tools import Calculate
        with reference_context(self.service, turn):
            numeric = await Calculate().call(expression='1+1')
        self.assertIn('EXECUTION_POLICY_BLOCKED', numeric.content[0].text)
        with self.assertRaises(AnalysisError) as caught:
            await self.declare(executionPolicy={'sourceQueries':True})
        self.assertEqual(caught.exception.code, 'EXECUTION_POLICY_CONFLICT')
        new = self.turn('现在查询新的指标')
        await self.declare(executionPolicy={'sourceQueries':True})
        self.assertTrue(new.state['executionPolicy']['sourceQueries'])
