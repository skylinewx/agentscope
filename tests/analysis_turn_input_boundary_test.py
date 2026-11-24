"""Budget turns use explicit historical references and the active message boundary."""
from types import SimpleNamespace
from unittest import TestCase

from analysis_extension.history_references import bind, execution_view, trace_markdown
from analysis_extension.model_materials import current_turn_input
from analysis_extension.models import AnalysisError


class TurnInputBoundaryTests(TestCase):
    def test_old_business_content_is_independent_of_history_length(self):
        system = SimpleNamespace(role='system', id='system')
        current = SimpleNamespace(role='user', id='now')
        tool_pair = [SimpleNamespace(role=role, id=role) for role in ('assistant', 'tool')]
        tools = [{'function': {'name': name}} for name in ('AnalysisTask', 'AnalysisBuild')]
        for count in (1, 20):
            previous = [SimpleNamespace(role='user', id=f'old-{i}') for i in range(count)]
            package = {'messages': [system, *previous, current, *tool_pair], 'tools': tools}
            fresh = current_turn_input({'userMessageIds': ['now']}, package)
            self.assertEqual(fresh['messages'], [system, current, *tool_pair])
            self.assertEqual(fresh['tools'], tools[:1])
            declared = current_turn_input({'mode': 'ANALYSIS', 'userMessageIds': ['now']}, package)
            self.assertEqual(declared['tools'], tools)
            self.assertEqual(len(package['messages']), count + 4)
        compressed = {'messages': [system, *tool_pair], 'tools': tools}
        self.assertEqual(current_turn_input({'mode': 'DIRECT', 'userMessageIds': ['now']}, compressed), compressed)

    def test_execution_trace_requires_selected_conversation_record_and_preserves_failure(self):
        records = [{'contextRef': ref, 'request': name, 'turnKey': ref, 'status': 'INCOMPLETE',
                    'executionCaptureRefs': [], 'toolFailures': [{'code': 'DEPENDENCY_TIMEOUT'}]}
                   for ref, name in [('a', '查询甲'), ('b', '查询乙')]]
        turn = SimpleNamespace(state={'request': '解释前面的查询', 'businessContexts': records})
        with self.assertRaises(AnalysisError) as caught:
            bind(turn, [{'use': 'QUERY_TRACE', 'mention': '前面的查询'}])
        self.assertEqual(caught.exception.code, 'HISTORY_CONTEXT_SELECTION')
        chosen = bind(turn, [{'use': 'QUERY_TRACE', 'mention': '前面的查询', 'contextRef': 'a'}])
        self.assertEqual([item['contextRef'] for item in chosen], ['a'])
        material = trace_markdown(execution_view(turn, 'a'))
        self.assertIn('查询甲', material)
        self.assertIn('DEPENDENCY_TIMEOUT', material)
        self.assertNotIn('查询乙', material)
        with self.assertRaises(AnalysisError):
            execution_view(turn, 'another-session-context')

    def test_publication_schemas_follow_actual_artifacts_and_explicit_requests(self):
        from analysis_extension.turn_completion import TurnCompletion
        tools = [{'function': {'name': name}} for name in
                 ('AnalysisTask', 'AnalysisBuild', 'AnalysisInspect', 'SomeOtherSource', 'AnalysisPublish', 'AnalysisRenderReport')]
        package = {'messages': [], 'tools': tools}
        turn = TurnCompletion(SimpleNamespace(), {'mode': 'DIRECT', 'buildRefs': []})
        self.assertEqual(current_turn_input(turn.state, package)['tools'], tools[:4])
        for key in ('adoptedReportRef', 'analysisRef', 'buildRefs'):
            self.assertEqual(current_turn_input({**turn.state, key: ['saved']}, package)['tools'], tools)
        self.assertEqual(turn.task(action='tools')['status'], 'READY')
        self.assertEqual(current_turn_input(turn.state, package)['tools'], tools)
        self.assertEqual(current_turn_input({'mode': 'ANALYSIS'}, package)['tools'], tools[:4])

    def test_archived_failure_exposes_the_service_reason_without_requery(self):
        from analysis_extension.conversation_context import save_context
        state = {'turnKey': 'failed', 'request': '读取已知目标', 'failures': {'attempt': {
            'tool': 'execute_budget_query', 'state': 'error', 'error': {'code': 'DEPENDENCY_TIMEOUT',
            'message': '预算接口超时'}, 'request': {'unrelated': 'private-payload'}}}}
        save_context(state, 'INCOMPLETE')
        record = state['businessContexts'][0]
        wire = trace_markdown(execution_view(SimpleNamespace(state=state), record['contextRef']))
        self.assertIn('预算接口超时', wire)
        self.assertNotIn('private-payload', wire)

    def test_repeated_pronoun_in_an_old_question_is_not_a_source_identity(self):
        records = [{'contextRef': 'a', 'request': '分析这些企业的收入'}, {'contextRef': 'b', 'request': '查询新入选的企业'}]
        turn = SimpleNamespace(state={'request': '解释这些企业是怎么查的', 'businessContexts': records})
        with self.assertRaises(AnalysisError) as error:
            bind(turn, [{'use': 'QUERY_TRACE', 'mention': '这些企业'}])
        self.assertEqual(error.exception.code, 'HISTORY_CONTEXT_SELECTION')
        turn.state['request'] = '解释“分析这些企业的收入”的查询过程'
        selected = bind(turn, [{'use': 'QUERY_TRACE', 'mention': '分析这些企业的收入'}])
        self.assertEqual(selected[0]['contextRef'], 'a')
