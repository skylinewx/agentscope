import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock

from analysis_extension.model_materials import current_turn_input, remember_catalog_tool
from analysis_extension.models import AnalysisError
from analysis_extension.objectives import merge, for_selection, resolve_dependencies
from analysis_extension.query_context import QueryContexts


class QueryContextTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.contexts = QueryContexts()
        self.origin = {'resourceId': 'lease', 'serverName': 'server', 'connectionFingerprint': 'connection'}
        self.store = Mock()
        self.store.get_capture.side_effect = lambda owner, ref: {'response': self.captures[ref]}
        adapter = SimpleNamespace(raw_receipt=lambda value: value)
        sources = SimpleNamespace(for_capture=lambda capture: adapter)
        self.turn = SimpleNamespace(state={}, service=SimpleNamespace(owner='owner', store=self.store, sources=sources))
        self.captures, self.calls = {}, []
        self.entry = {'sourceId': 'budget', 'snapshotId': 'snapshot', 'queryable': True}
        self.pages = {None: {'directory': {'revision': '1', 'entries': [self.entry]}}}

    async def runner(self, turn, origin, tool, args):
        self.assertIs(turn, self.turn)
        self.assertEqual((origin, tool), (self.origin, 'get_context'))
        action = args['action']
        self.calls.append(action)
        ref = str(len(self.calls))
        await asyncio.sleep(0)
        if action == 'discover':
            self.captures[ref] = self.pages[args.get('contextDiscoveryCursor')]
        else:
            self.captures[ref] = {'session': {'context': {**self.entry, 'contextId': 'c'}, 'querySessionId': 's',
                'expiresAt': (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}}
        return [ref], None

    async def bind(self, request=None):
        return await self.contexts.bind(self.turn, self.origin, request or {}, self.runner)

    async def test_unique_after_all_pages_opens_once_in_parallel(self):
        self.pages[None]['directory'].update(entries=[], nextCursor='page2')
        self.pages['page2'] = {'directory': {'revision': '1', 'entries': [self.entry]}}
        first, second = await asyncio.gather(self.bind(), self.bind())
        self.assertEqual(first, {'contextId': 'c', 'querySessionId': 's'})
        self.assertEqual(second, first)
        self.assertEqual(self.calls, ['discover', 'discover', 'open'])
        next(iter(self.turn.state['budgetQueryContexts'].values()))['expiresAt'] = '2000-01-01T00:00:00+00:00'
        await self.bind()
        self.assertEqual(self.calls.count('open'), 2)

    async def test_ambiguous_or_changed_directory_never_opens_first(self):
        self.pages[None]['directory']['nextCursor'] = 'page2'
        self.pages['page2'] = {'directory': {'revision': '1', 'entries': [{**self.entry, 'snapshotId': 'other'}]}}
        with self.assertRaises(AnalysisError) as ambiguous:
            await self.bind()
        self.assertEqual(ambiguous.exception.code, 'QUERY_SOURCE_SELECTION')
        self.assertEqual(len(ambiguous.exception.details['candidates']), 2)
        self.pages['page2']['directory']['revision'] = '2'
        with self.assertRaises(AnalysisError) as changed:
            await self.bind()
        self.assertEqual(changed.exception.code, 'QUERY_DIRECTORY_CHANGED')
        self.assertNotIn('open', self.calls)

    async def test_only_frozen_facts_inherit_their_context_without_discovery(self):
        self.turn.state.update(executionPolicy={'sourceQueries': False}, historyReferences=[
            {'role': 'PRIMARY', 'use': 'FACTS', 'sourceRef': 'historical'}])
        self.store.get_source_view.return_value = {'sourceIdentity': self.origin, 'selection': {'captureRef': 'old'}}
        self.captures['old'] = {'context': {'contextId': 'old-c'}, 'querySessionId': 'old-s'}
        self.assertEqual(await self.bind(), {'contextId': 'old-c', 'querySessionId': 'old-s'})
        self.turn.state['historyReferences'][0]['use'] = 'MEMBERS'
        with self.assertRaises(AnalysisError) as blocked:
            await self.bind()
        self.assertEqual(blocked.exception.code, 'EXECUTION_POLICY_BLOCKED')
        self.assertEqual(self.calls, [])


class StageTests(TestCase):
    def test_native_selection_error_distinguishes_business_rows_and_filtered_physical_rows(self):
        from unittest.mock import patch
        from analysis_extension.adapters.budget_batch import whole_metric_selection
        from analysis_extension.batch_scope import resolve_members
        entry = {'captureRef': 'c', 'stepId': 'step', 'batchId': 'b', 'columnNames': ['x','y'],
                 'rowIndicesOptional': True, 'disposition': 'SUCCEEDED'}
        selection = {'captureRef': 'c', 'stepId': 'step', 'batchId': 'b', 'columnNames': ['x'], 'rowIndices': [1]}
        table = {'data': {'columns': []}, 'exactRowMetrics': ['x','y']}
        self.assertEqual(whole_metric_selection(entry, 'PRIMARY')['columnNames'], ['x','y'])
        for optional in (True, False):
            item = {**entry, 'rowIndicesOptional': optional}
            with patch('analysis_extension.batch_scope.selected_tables', return_value=[(selection, table, '', {'x'}, set())]), \
                 patch('analysis_extension.adapters.budget_batch.selectors', return_value=[item]), \
                 self.assertRaises(AnalysisError) as caught:
                resolve_members(Mock(), {'c': {}}, [selection], {'memberDimension': 'dimension'}, 100)
            self.assertEqual(caught.exception.code, 'PREPARATION_NEEDS_SELECTION')
            details = caught.exception.details
            self.assertEqual(details['nativeRowsForColumns'], 1)
            self.assertEqual('ifUsingAllReturnedRows' in details, optional)
            self.assertEqual(whole_metric_selection(item,'REFERENCE') is not None, optional)
            if optional:
                self.assertNotIn('rowIndices', details['ifUsingAllReturnedRows'])

    def test_ready_report_tools_and_explicit_catalog(self):
        names = ['AnalysisTask', 'AnalysisBuild', 'AnalysisPublish', 'AnalysisPython', 'AnalysisInspect',
                 'QueryResultRead', 'source-query', 'extra']
        model = {'messages': [], 'tools': [{'name': k} for k in names]}
        def visible(**state):
            result = current_turn_input({'mode': 'ANALYSIS', **state}, model, {'source-query': 'execute_budget_query'})
            return {tool['name'] for tool in result['tools']}
        self.assertNotIn('AnalysisBuild', visible())
        self.assertIn('source-query', visible())
        self.assertIn('AnalysisBuild', visible(queryCaptureRefs=['receipt']))
        self.assertNotIn('source-query', visible(analysisRef='report'))
        self.assertEqual(visible(adoptedReportRef='report'), {'AnalysisTask', 'AnalysisPublish', 'AnalysisInspect', 'QueryResultRead'})
        self.assertEqual(visible(analysisRef='report', expandedToolCatalog=True), set(names))
        state = {'analysisRef': 'report', 'expandedToolCatalog': True}
        remember_catalog_tool(state, 'extra')
        self.assertIn('extra', visible(**state))
        self.assertNotIn('source-query', visible(**state))

    def test_pending_objective_cannot_expand_whole_source_and_rebind_uses_selected_members(self):
        from unittest.mock import patch
        state = {'turnKey': 'turn'}
        values = {'capability': 'VALUES', 'question': '满足条件的站点'}
        dependent = {'capability': 'DATA_COMPOSITION', 'question': '这些站点的产出', 'membersFromObjective': 1}
        state['objectives'] = merge(state, [values, dependent])
        analysis = state['objectives'][1]
        self.assertEqual(for_selection(state, {'role': 'PRIMARY', 'sourceRef': 'all'}), [])
        with patch('analysis_extension.objectives.dependency_sources', return_value=[
            {'sourceRef': k, 'rowCount': 2, 'label': k} for k in ('one', 'two')]):
            turn = SimpleNamespace(state=state)
            with self.assertRaises(AnalysisError) as ambiguous:
                resolve_dependencies(turn)
            self.assertEqual(ambiguous.exception.code, 'OBJECTIVE_MEMBER_SELECTION')
            for ref in ('one', 'two'):
                analysis['memberSourceRef'] = ref
                resolve_dependencies(turn)
                self.assertEqual(analysis['sourceRef'], ref)
                self.assertEqual(for_selection(state, {'role': 'PRIMARY', 'sourceRef': ref}), [analysis])
        with self.assertRaises(AnalysisError):
            merge(state, [{**dependent, 'membersFromObjective': {}}])
        both = merge({'turnKey':'another'}, [values, {**values, 'question':'另一批站点'},
                                             dependent, {**dependent, 'membersFromObjective':2}])
        self.assertEqual(len(both), 4)
        self.assertNotEqual(both[2]['objectiveRef'], both[3]['objectiveRef'])
