from copy import deepcopy
from analysis_presentation_fixtures import PresentationCase
from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_fixtures import OWNER
from analysis_extension.models import AnalysisError, Owner
from analysis_extension.source_views import register, directory
from analysis_extension.turn_completion import TurnCompletion
from analysis_extension.conversation_context import next_context_state
from analysis_extension.followup_scope import remember_subject


class SourceViewTests(PresentationCase):
    async def test_searching_a_comparison_question_includes_its_reused_subject(self):
        ref = self.prepare_sources()
        raw = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        selections = []
        for role, selected in [('PRIMARY', {**raw, 'rowIndices': [0]}), ('REFERENCE', raw)]:
            view = register(self.service.turn, selected)
            selections.append({**selected, 'role': role, 'sourceRef': view['sourceRef']})
        scope = {'scopeRef': 'selected-groups', 'selections': selections}
        record = {'contextRef': 'compared', 'request': '以选定对象为主体和全体比较', 'subjectScope': scope}
        turn = TurnCompletion(self.service, {'businessContexts': [record], 'inheritedScope': scope})
        catalog = directory(turn, search='选定对象')
        self.assertEqual(catalog['total'], 2)
        self.assertIn('（主体） | 1 |', catalog['markdown'])
        self.assertIn('（对照） | 2 |', catalog['markdown'])
        self.assertEqual(directory(turn, search='另一业务')['total'], 0)
        self.assertEqual(directory(turn, search='选定对象', context_refs=[])['total'], 0)
        self.assertNotIn('adoptedInputs', turn.state)

    async def test_comparison_context_accepts_only_sources_selected_from_its_directory(self):
        ref = self.prepare_sources()
        scope = {'selections': [], 'basis':'UNRESOLVED_QUERY_SUBJECT', 'candidateCount':2, 'captureRefs':[ref]}
        record = {'turnKey':'previous', 'contextRef':'ctx-old', 'request':'查两项数值', 'goal':'查两项数值', 'kind':'DELIVERED',
                  'status':'DIRECT', 'subjectScope':scope, 'captureRefs':[ref], 'intent':{}}
        turn = self.service.turn = TurnCompletion(self.service, {'turnKey':'current', 'request':'比较刚才两项',
            'businessContexts':[record], 'inheritedScope':scope, 'failures':{}, 'buildRefs':[]})
        refs = [s['sourceRef'] for s in directory(turn, context_refs=['ctx-old'])['sources']]
        self.assertEqual(len(refs), 2)
        comparison = {'items':[{'sourceRef':ref} for ref in refs]}
        with self.assertRaises(AnalysisError):
            await self.service.task(mode='DIRECT', comparison=comparison,
                                    context={'mode':'CONTINUE','contextRefs':['unknown']})
        await self.service.task(mode='DIRECT', comparison=comparison, context={'mode':'CONTINUE','contextRefs':refs})
        self.assertEqual(turn.state['followupContext']['contextRefs'], ['ctx-old'])
        self.assertEqual({i['sourceRef'] for i in turn.state['comparisonPlan']['items']}, set(refs))

    async def test_exact_cell_subject_freezes_real_rows_and_retains_selected_table_proof(self):
        from analysis_exact_cells_test import exact_fixture
        from analysis_extension.query_subject import candidates, freeze_subject
        from analysis_extension.query_lineage import selected_result_sets, scope_terms
        capture, receipt = exact_fixture()
        ref = self.store.capture(OWNER, capture)
        self.service.turn = TurnCompletion(self.service, {'turnKey': 'exact', 'request': '查询结果',
            'queryCaptureRefs': [ref], 'failures': {}, 'buildRefs': []})
        turn = self.service.turn
        available = candidates(turn)
        self.assertTrue(available)
        self.assertTrue(all(item['rowCount'] == 4 for item in available))
        selected = next(item for item in available if 'metric1' in item['columnNames'])
        freeze_subject(turn, available, [{'candidateRef': selected['candidateRef'], 'rowIndices': [1]}])
        self.assertFalse(turn.state['queryAnswerSubject']['invalidSelection'])
        selection = turn.state['queryAnswerSubject']['selections'][0]
        self.assertEqual([item['resultSetId'] for item in selected_result_sets(receipt, selection)], ['r0'])
        turn.state.update(followupContext={'mode': 'CONTINUE'}, selectedContextRefs=['origin'], inheritedScope={'selections': [selection]})
        self.assertEqual(scope_terms(turn, capture, {'scope': {'formNames': ['利润表']}}), ['利润表'])

    async def test_complete_empty_result_is_adopted_and_built_without_inventing_members(self):
        capture, receipt = business_fixture()
        receipt['data']['resultSets'][0]['rows'] = []
        update(capture, receipt)
        ref = self.store.capture(OWNER, capture)
        self.service.turn = TurnCompletion(self.service, {'turnKey': 'empty', 'request': '分析查询结果',
            'queryCaptureRefs': [ref], 'failures': {}, 'buildRefs': []})
        sources = directory(self.service.turn)['sources']
        empty = next(item for item in sources if item['rowCount'] == 0)
        await self.service.task(mode='ANALYSIS', goal='分析查询结果', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}],
                                subjectSources=[{'sourceRef': empty['sourceRef'], 'role': 'PRIMARY'}])
        built = await self.service.build(operations=[], requestKey='empty')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(artifact['facts'], [])
        self.assertEqual(artifact['scope']['members'], [])
        self.assertTrue(artifact['scope']['selectedDataComplete'])
        self.assertEqual(artifact['scope']['inputGroups'][0]['sourceRef'], empty['sourceRef'])
        from analysis_extension.source_views import query_basis
        actual = query_basis(self.service.turn, capture, {'purpose': 'QUERY', 'basis': {
            'kind': 'MEMBERS', 'sourceRef': empty['sourceRef']}})
        self.assertEqual(actual['basis']['source']['factIds'], [])
        self.assertEqual(actual['basis']['source']['resultSetId'], 'r0')
        self.assertEqual(actual['basis']['memberDimensions'], [])
        # Empty FACTS follows the same immutable request normalization as
        # nonempty FACTS; inherited semantic targets must not trigger a query.
        facts = query_basis(self.service.turn, capture, {'purpose': 'QUERY', 'basis': {
            'kind': 'FACTS', 'sourceRef': empty['sourceRef']}, 'intentDraft': {
                'targets': [{'id': 'old', 'businessPhrases': []}], 'sharedConditions': []}})
        self.assertFalse(facts.get('intentDraft', {}).get('targets'))
        from analysis_extension.query_lineage import scope_terms
        self.service.turn.state['followupContext'] = {'mode': 'CONTINUE'}
        self.assertEqual(scope_terms(self.service.turn, capture, {'scope': {'formNames': ['利润表']}}), ['利润表'])

    async def test_columns_share_validation_but_new_directory_checks_changed_evidence(self):
        from unittest.mock import patch
        from analysis_extension.adapters.budget_batch import validate
        self.prepare_sources()
        with patch('analysis_extension.adapters.budget_batch.validate', wraps=validate) as checked:
            first = directory(self.service.turn)
        # Candidate discovery and directory registration each validate a table;
        # individual metric columns do not repeat the complete receipt checks.
        self.assertEqual(checked.call_count, 2)
        self.assertEqual(first['total'], 2)
        again = directory(self.service.turn)
        self.assertEqual(first, again)
        ref = self.service.turn.state['queryCaptureRefs'][0]
        capture = self.store.get_capture(OWNER, ref)
        capture['response']['content'][0]['text'] = '{}'
        with patch.object(self.store, 'get_capture', return_value=capture):
            invalid = directory(self.service.turn)
        self.assertEqual(invalid['total'], 0)
        self.assertIn(ref, self.service.turn.state['querySubjectDiagnostics'])

    async def test_source_registration_is_atomic_and_preserves_first_record(self):
        ref = self.prepare_sources()
        source = directory(self.service.turn)['sources'][0]
        saved = self.store.get_source_view(OWNER, source['sourceRef'])
        changed = {**saved, 'label': 'changed label'}
        self.assertEqual(self.store.save_source_views(OWNER, [changed])[0], saved)
        foreign = Owner('another', OWNER.agent_id, OWNER.session_id)
        with self.assertRaises(AnalysisError):
            self.store.save_source_views(foreign, [saved])
        invalid = deepcopy(saved)
        invalid['selection']['captureRef'] = 'unknown-capture'
        with self.assertRaises(AnalysisError):
            self.store.save_source_views(OWNER, [changed, invalid])
        self.assertEqual(self.store.get_source_view(OWNER, saved['sourceRef']), saved)

    def prepare_sources(self):
        capture, _ = business_fixture()
        ref = self.store.capture(OWNER, capture)
        state = {'turnKey': 'sources', 'request': '分别分析两个结果', 'failures': {}, 'buildRefs': [],
                 'queryCaptureRefs': [ref], 'inheritedScope': {}}
        self.service.turn = TurnCompletion(self.service, state)
        return ref

    async def test_followup_keeps_confirmed_subset_and_full_reference_on_the_same_source(self):
        from unittest.mock import patch
        from analysis_extension.followup_scope import _confirmed_primary_rows
        from analysis_extension.source_views import install, select
        ref = self.prepare_sources()
        turn = self.service.turn
        raw = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        source = register(turn, raw)
        inputs = select(turn, [{'sourceRef': source['sourceRef'], 'role': role} for role in ('PRIMARY', 'REFERENCE')])
        install(turn, inputs)
        turn.state['inheritedScope'] = {'selections': [{**raw, 'rowIndices': [1]}]}
        objective = {'role': 'PRIMARY', 'capability': 'DATA_COMPOSITION', 'sourceRef': source['sourceRef'], 'factRefs': ['f1']}
        facts = [{'factId': 'f' + str(i), 'rowIndex': i, 'stepId': 'batch', 'batchId': 'batch-A',
                  'columnName': 'metric1', 'captureRefs': [ref],
                  'inputMemberships': [{'inputRef': inputs[0]['inputRef'], 'inputRole': 'PRIMARY'}]} for i in (0, 1)]
        artifact = {'captureRefs': [ref], 'computationIntent': {'objectives': [objective]}, 'facts': facts}
        self.assertEqual(_confirmed_primary_rows(turn, artifact, inputs), (inputs[0]['inputRef'], [1]))
        for goals in ([{**objective, 'factRefs': ['f0']}], [{**objective, 'factRefs': ['other']}],
                      [objective, {**objective, 'capability': 'VALUES', 'factRefs': []}]):
            self.assertIsNone(_confirmed_primary_rows(turn, {**artifact, 'computationIntent': {'objectives': goals}}, inputs))
        with patch.object(self.store, 'get_analysis', return_value=artifact):
            remember_subject(turn, {'status': 'READY', 'analysisRef': 'saved'})
        scope = turn.state['resolvedSubjectScope']['selections']
        self.assertEqual([(s['role'], s.get('rowIndices')) for s in scope], [('PRIMARY', [1]), ('REFERENCE', None)])
        self.assertNotEqual(scope[0]['sourceRef'], scope[1]['sourceRef'])
        turn.state.update(queryCaptureRefs=[], inheritedScope={'selections': scope})
        catalog = directory(turn)
        self.assertIn('（主体）', catalog['markdown'])
        self.assertIn('（对照）', catalog['markdown'])

    async def test_explicit_members_bind_the_population_without_adopting_its_old_amounts(self):
        ref = self.prepare_sources()
        source = directory(self.service.turn)['sources'][0]
        selection = self.store.get_source_view(OWNER, source['sourceRef'])['selection']
        record = {'turnKey': 'sources', 'contextRef': 'old', 'request': '筛出对象', 'kind': 'DELIVERED',
                  'status': 'DIRECT', 'intent': {}, 'subjectScope': {'selections': [selection]}, 'captureRefs': [ref]}
        turn = self.service.turn = TurnCompletion(self.service, {'turnKey': 'next', 'request': '查询这些对象的产出',
            'businessContexts': [record, {**record, 'contextRef': 'other', 'captureRefs': [], 'subjectScope': {}}],
            'failures': {}, 'buildRefs': []})
        declared = await self.service.task(mode='DIRECT', references=[{'use': 'MEMBERS', 'mention': '这些对象',
            'sourceRef': source['sourceRef']}])
        self.assertEqual(declared['historyReferences'][0]['sourceRef'], source['sourceRef'])
        self.assertFalse(turn.state.get('adoptedInputs'))
        await self.service.task(mode='DIRECT', references=[{'use': 'FACTS', 'mention': '这些对象', 'sourceRef': source['sourceRef']}])
        self.assertEqual([item['sourceRef'] for item in turn.state['adoptedInputs']], [source['sourceRef']])
        await self.service.task(mode='DIRECT', references=[{'use': 'MEMBERS', 'mention': '这些对象', 'sourceRef': source['sourceRef']}])
        self.assertFalse(turn.state.get('adoptedInputs'))

    async def test_reusing_a_named_source_path_keeps_only_its_native_form_or_task(self):
        from analysis_extension.followup_scope import prepare_request
        from analysis_extension.history_references import apply_source_paths
        ref = self.prepare_sources()
        sources = directory(self.service.turn)['sources']
        selections = [self.store.get_source_view(OWNER, s['sourceRef'])['selection'] for s in sources]
        origin = self.store.get_capture(OWNER, ref)
        ctx = {'contextRef': 'old', 'turnKey': 'sources', 'kind': 'DELIVERED', 'request': '已查询两项科目',
               'status': 'DIRECT', 'captureRefs': [ref], 'subjectScope': {'selections': selections}}
        for use, phrase, field, expected in [('FORM', '上次那张表', 'formCodes', 'LRB'),
                                             ('TASK', '原来的任务', 'taskCodes', 'TASK')]:
            turn = self.service.turn = TurnCompletion(self.service, {'turnKey': 'next', 'request': phrase + '查收入',
                'businessContexts': [ctx], 'failures': {}, 'buildRefs': []})
            declared = await self.service.task(mode='DIRECT', references=[{'use': use, 'mention': phrase}])
            self.assertEqual([item['code'] for item in declared['historyReferences']], [expected])
            self.assertFalse(turn.state.get('adoptedInputs'))
            prepared = prepare_request(turn, origin, {'intentDraft': {'targets': [
                {'id': 'income', 'businessPhrases': [{'text': '收入'}]}]}})
            self.assertEqual(prepared['scope'], {field: [expected]})
            self.assertFalse(prepared.get('basis'))
            self.assertEqual(prepared['intentDraft']['targets'][0]['id'], 'income')
            with self.assertRaises(AnalysisError) as conflict:
                apply_source_paths(turn, origin, {'scope': {field: ['different']}}, 'PRIMARY')
            self.assertEqual(conflict.exception.code, 'HISTORY_SCOPE_CONFLICT')
            with self.assertRaises(AnalysisError) as foreign:
                apply_source_paths(turn, {**origin, 'connectionFingerprint': 'another'}, {}, 'PRIMARY')
            self.assertEqual(foreign.exception.code, 'SOURCE_CONNECTION_MISMATCH')
            with self.assertRaises(AnalysisError):
                await self.service.task(mode='DIRECT', references=[{'use': use, 'mention': phrase, 'sourceCode': 'unknown'}])

    async def test_directory_is_read_only_and_sources_are_owner_bound(self):
        self.prepare_sources()
        result = directory(self.service.turn)
        self.assertEqual(result['total'], 2)
        self.assertNotIn('adoptedInputs', self.service.turn.state)
        from analysis_extension.business_tables import model_text
        rendered = model_text(result)
        self.assertTrue(rendered.startswith('| 来源引用 |'))
        self.assertNotIn('baseContext', rendered)
        for item in result['sources']:
            self.assertEqual(self.store.get_source_view(OWNER, item['sourceRef'])['sourceRef'], item['sourceRef'])
            with self.assertRaises(AnalysisError):
                self.store.get_source_view(Owner('someone-else', 'agent', 'session'), item['sourceRef'])

    async def test_two_primaries_build_and_resume_as_separate_groups(self):
        self.prepare_sources()
        sources = directory(self.service.turn)['sources']
        declared = await self.service.task(mode='ANALYSIS', goal='分别分析两个结果', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}],
            subjectSources=[{'sourceRef': item['sourceRef'], 'role': 'PRIMARY'} for item in sources])
        inputs = declared['subjectSources']
        built = await self.service.build(operations=[{'id': 'count' + str(i), 'operator': 'COUNT',
            'select': {'inputRef': item['inputRef']}} for i, item in enumerate(inputs)], requestKey='two-primary')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['scope']['inputGroups']), 2)
        self.assertTrue(all(value['calculationValidation'] == 'PASS' for value in artifact['derivedValues']))
        remember_subject(self.service.turn, {'status': 'READY', 'analysisRef': built['analysisRef']})
        restored = next_context_state(deepcopy(self.service.turn.state))
        self.assertNotIn('adoptedInputs', restored)
        self.assertEqual(len(restored['businessContexts'][-1]['adoptedInputs']), 2)

    async def test_direct_build_remains_reviewable_after_result_is_indexed_next_turn(self):
        from unittest.mock import patch, AsyncMock
        from analysis_extension.input_state import validate_analysis
        capture, _ = business_fixture()
        capture.update(serverName='budget-source', connectionFingerprint='authorized-connection')
        ref = self.store.capture(OWNER, capture)
        self.service.turn = TurnCompletion(self.service, {'turnKey': 'direct-build', 'request': '分析两个科目',
            'queryCaptureRefs': [ref], 'failures': {}, 'buildRefs': []})
        from analysis_evidence_closure_test import EvidenceClosureTests
        self.service.turn.state['queryCaptureRefs'].append(EvidenceClosureTests.verify_capture(self, ref))
        await self.service.task(mode='ANALYSIS', goal='分析两个科目', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}])
        built = await self.service.build([ref], [{'captureRef': ref, 'stepId': 'batch',
            'batchId': 'batch-A', 'columnNames': ['metric1', 'metric2'], 'role': 'PRIMARY'}],
            {'label': '明确选中的原结果', 'selectionReason': '两个科目'}, [], 'direct-build')
        calculated = await self.service.python(inputSetRef=built['inputSetRef'],
            code="result=[{'name':'twice','label':'单项倍数','value':D(inputs[0]['value'])*2,'unit':'元','inputRefs':[inputs[0]['ref']]}]",
            purpose='独立结果', requestKey='direct-python')
        artifact = self.store.get_analysis(OWNER, calculated['analysisRef'])
        self.assertIsNone(artifact['plan'].get('inputSetVersion'))
        remember_subject(self.service.turn, {'status': 'READY', 'analysisRef': calculated['analysisRef']})
        restored = next_context_state(deepcopy(self.service.turn.state))
        restored.update(turnKey='review', request='继续审核，不取数、不计算', failures={}, buildRefs=[])
        self.service.turn = TurnCompletion(self.service, restored)
        self.assertNotIn('adoptedInputs', restored)
        await self.service.task(mode='REPORT', goal='审核原报告', references=[{'use': 'REPORT', 'mention': '继续审核'}],
            executionPolicy={'sourceQueries': False, 'computations': False})
        self.assertEqual(len(self.service.turn.state['adoptedInputs']), 2)
        from jsonschema import validate
        from analysis_extension.tools import AnalysisTask
        lookup = {'action': 'tools', 'analysisRef': calculated['analysisRef']}
        validate(lookup, AnalysisTask(self.service).model_input_schema)
        original = deepcopy(self.service.turn.state)
        self.assertEqual((await self.service.task(**lookup))['status'], 'READY')
        self.assertEqual(self.service.turn.state, {**original, 'expandedToolCatalog': True})
        with self.assertRaises(AnalysisError):
            await self.service.task(action='tools', analysisRef='ana_unknown:1')
        before = deepcopy(self.service.turn.state)
        validate_analysis(self.service, artifact)
        self.assertEqual(self.service.turn.state, before)
        # A model may mistakenly adopt one metric preview while reviewing the
        # complete report. The explicit report reference restores every input
        # without executing its query or calculations again.
        partial = self.service.turn.state['adoptedInputs'][0]
        await self.service.task(mode='REPORT', goal='审核原报告',
            subjectSources=[{'sourceRef': partial['sourceRef']}])
        with self.assertRaises(AnalysisError) as wrong_input:
            validate_analysis(self.service, artifact)
        self.assertEqual(wrong_input.exception.details['restoreOriginalReportInputs']['arguments']['analysisRef'], artifact['analysisRef'])
        with patch('analysis_extension.service.calculate') as no_calculation, \
             patch.object(self.service.budget_preparation, '_fetch', new_callable=AsyncMock) as no_query:
            restored_report = await self.service.task(mode='REPORT', goal='审核原报告', analysisRef=artifact['analysisRef'])
            no_calculation.assert_not_called()
            no_query.assert_not_awaited()
        self.assertEqual(restored_report['reusedAnalysis']['sourceCount'], 2)
        self.assertEqual(self.service.turn.state['adoptedReportRef'], artifact['analysisRef'])
        self.assertNotIn('subjectSources', restored_report)
        validate_analysis(self.service, artifact)
        derived = next(value for value in artifact['derivedValues'] if value['operator'] == 'PYTHON')
        doc = {'title': '审核结论', 'html': '<html><head></head><body><p>复用原结果 {{value}}</p></body></html>',
               'bindings': [{'key': 'value', 'valueRef': derived['derivedId']}]}
        with patch('analysis_extension.service.calculate') as calculate, \
             patch('analysis_extension.python_analysis.execute_python', new_callable=AsyncMock) as execute, \
             patch.object(self.service.budget_preparation, '_fetch', new_callable=AsyncMock) as fetch:
            preview = await self.service.publish(calculated['analysisRef'], document=doc,
                requestKey='review-existing', previewOnly=True)
            calculate.assert_not_called()
            execute.assert_not_awaited()
            fetch.assert_not_awaited()
        self.assertTrue(preview['previewOnly'])
        self.assertEqual(self.store.get_analysis(OWNER, calculated['analysisRef']), artifact)
        # A later direct query can use the same rows. An old report repair proposal
        # still belongs to its original report, even if the input versions compare equal.
        report_state = deepcopy(self.service.turn.state)
        self.service.turn.state.update(mode='DIRECT', adoptedReportRef=None, buildRefs=[],
                                      queryCaptureRefs=[ref], queryResultRefs=['current-result'])
        with self.assertRaises(AnalysisError) as obsolete:
            await self.service.publish(repairRef=preview['repairRef'], repairs=[], previewOnly=True)
        self.assertEqual(obsolete.exception.code, 'STALE_INPUT_SET')
        self.assertEqual(obsolete.exception.details, {'currentMode': 'DIRECT'})
        self.service.turn.state['adoptedInputs'][0]['selection']['role'] = 'REFERENCE'
        self.service.turn.state['inputSetVersion'] = 'new-inputs'
        with self.assertRaises(AnalysisError) as mismatch:
            validate_analysis(self.service, artifact)
        self.assertEqual(mismatch.exception.details, {'currentMode': 'DIRECT'})
        self.service.turn.state = report_state
        # A role, column, row or grain change is still a real input change.
        selected = deepcopy(self.service.turn.state['adoptedInputs'])
        for change in ({'role': 'REFERENCE'}, {'columnNames': ['metric2']},
                       {'rowIndices': [0]}, {'grain': ['MD_ORG']}):
            with self.subTest(change=change):
                self.service.turn.state['adoptedInputs'] = deepcopy(selected)
                self.service.turn.state['adoptedInputs'][0]['selection'].update(change)
                with self.assertRaises(AnalysisError) as error:
                    validate_analysis(self.service, artifact)
                self.assertEqual(error.exception.code, 'STALE_INPUT_SET')

    async def test_ambiguous_context_directory_exposes_candidates_without_adopting(self):
        ref = self.prepare_sources()
        # Multiple result groups were delivered without selecting a sole base.
        # The next round must still be able to discover and choose either one.
        capture, receipt = business_fixture()
        receipt['data']['resultSets'][1]['rows'] = receipt['data']['resultSets'][1]['rows'][:1]
        update(capture, receipt)
        ref = self.store.capture(OWNER, capture)
        scope = {'selections': [], 'basis': 'UNRESOLVED_QUERY_SUBJECT', 'candidateCount': 2, 'captureRefs': [ref]}
        self.service.turn.state.update(queryCaptureRefs=[], inheritedScope=scope,
            businessContexts=[{'contextRef': 'old', 'subjectScope': scope, 'captureRefs': [ref]}])
        before = deepcopy(self.service.turn.state)
        for context_refs in (['old'], None):
            with self.subTest(context_refs=context_refs):
                result = directory(self.service.turn, context_refs=context_refs)
                self.assertEqual(result['total'], 2)
                self.assertEqual(sorted(item['rowCount'] for item in result['sources']), [1, 2])
                self.assertEqual(self.service.turn.state, before)
        # Explicitly choosing a subset must not expand it back into alternatives.
        chosen = self.store.get_source_view(OWNER, result['sources'][0]['sourceRef'])['selection']
        scope['selections'] = [chosen]
        self.assertEqual(directory(self.service.turn, context_refs=['old'])['total'], 1)

    async def test_overlap_does_not_duplicate_facts_and_replacement_is_exact(self):
        ref = self.prepare_sources()
        source = register(self.service.turn, {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A',
                                              'columnNames': ['metric1']})
        choices = [{'sourceRef': source['sourceRef'], 'role': role} for role in ('PRIMARY', 'REFERENCE')]
        declared = await self.service.task(mode='ANALYSIS', goal='主体和对照分别统计', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}], subjectSources=choices)
        built = await self.service.build(operations=[{'id': 'c' + str(i), 'operator': 'COUNT',
            'select': {'inputRef': item['inputRef']}} for i, item in enumerate(declared['subjectSources'])], requestKey='overlap')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['facts']), source['rowCount'])
        self.assertTrue(all(len(fact['inputMemberships']) == 2 for fact in artifact['facts']))
        old = self.service.turn.state['inputSetVersion']
        await self.service.task(mode='ANALYSIS', goal='仅分析主体', subjectSources=choices[:1])
        self.assertNotEqual(self.service.turn.state['inputSetVersion'], old)
        self.assertEqual(len(self.service.turn.state['adoptedInputs']), 1)
        from analysis_extension.input_state import validate_analysis
        with self.assertRaisesRegex(AnalysisError, '报告完整来源不同'):
            validate_analysis(self.service, artifact)

    async def test_rewording_goal_and_reordering_sources_keeps_data_version_and_preparation(self):
        self.prepare_sources()
        choices = [{'sourceRef': item['sourceRef']} for item in directory(self.service.turn)['sources']]
        await self.service.task(mode='ANALYSIS', goal='分析这两份结果', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}], subjectSources=choices)
        built = await self.service.build(operations=[], requestKey='unchanged-inputs')
        version = self.service.turn.state['inputSetVersion']
        self.service.turn.state['analysisRef'] = built['analysisRef']
        self.service.turn.state['budgetFormulaPreparation'] = {'kept': 'same data'}
        await self.service.task(mode='ANALYSIS', goal='换个角度比较同一份结果', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}],
                                subjectSources=list(reversed(choices)))
        self.assertEqual(self.service.turn.state['inputSetVersion'], version)
        self.assertEqual(self.service.turn.state['analysisRef'], built['analysisRef'])
        self.assertEqual(self.service.turn.state['budgetFormulaPreparation'], {'kept': 'same data'})
        self.assertTrue(all(item['selection']['selectionReason'] == '换个角度比较同一份结果'
                            for item in self.service.turn.state['adoptedInputs']))
        self.service.turn.state.pop('budgetFormulaPreparation')
        continued = await self.service.build(baseAnalysisRef=built['analysisRef'], operations=[], requestKey='same-data-next')
        self.assertEqual(self.store.get_analysis(OWNER, continued['analysisRef'])['plan']['inputSetVersion'], version)

    async def test_restored_context_keeps_existing_input_version_until_scope_changes(self):
        from analysis_extension.source_views import select, install
        self.prepare_sources()
        choices = [{'sourceRef': item['sourceRef']} for item in directory(self.service.turn)['sources']]
        await self.service.task(mode='ANALYSIS', goal='原目标', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}], subjectSources=choices)
        # Existing saved artifacts keep their version, even when created before
        # descriptive metadata was excluded from the input fingerprint.
        self.service.turn.state['inputSetVersion'] = 'saved-artifact-version'
        self.service.turn.state['queryAnswerSubject'] = {
            'selections': [deepcopy(item['selection']) for item in self.service.turn.state['adoptedInputs']],
            'basis': 'REVIEWED_ANSWER', 'candidateCount': len(choices)}
        remember_subject(self.service.turn, {'status': 'DIRECT'})
        self.assertEqual(self.service.turn.state['inputSetVersion'], 'saved-artifact-version')
        restored = next_context_state(deepcopy(self.service.turn.state))
        restored.update(turnKey='next', request='继续分析这些结果', failures={}, buildRefs=[])
        self.service.turn = TurnCompletion(self.service, restored)
        await self.service.task(mode='ANALYSIS', goal='新的解释目标', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}], subjectSources=choices)
        self.assertEqual(self.service.turn.state['inputSetVersion'], 'saved-artifact-version')
        changed = select(self.service.turn, [choices[0], {**choices[1], 'role': 'REFERENCE'}])
        install(self.service.turn, changed)
        self.assertNotEqual(self.service.turn.state['inputSetVersion'], 'saved-artifact-version')

    async def test_invalid_selection_cannot_partially_commit_declaration(self):
        self.prepare_sources()
        before = deepcopy(self.service.turn.state)
        with self.assertRaises(AnalysisError):
            await self.service.task(mode='DIRECT', goal='无效声明', intent={'period': '2025年'},
                                    subjectSources=[{'sourceRef': 'invented'}])
        self.assertEqual(before, self.service.turn.state)

    async def test_partial_analysis_retains_both_roles_after_a_successful_reference_query(self):
        ref = self.prepare_sources()
        turn = self.service.turn
        selection = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        primary = register(turn, {**selection, 'rowIndices': [0]})
        reference = register(turn, selection)
        await self.service.task(mode='ANALYSIS', goal='对照已选成员和全体成员', subjectSources=[
            {'sourceRef': primary['sourceRef'], 'role': 'PRIMARY'},
            {'sourceRef': reference['sourceRef'], 'role': 'REFERENCE'}])
        turn.state['queryAnswerSubject'] = {'selections': [selection], 'basis': 'REVIEWED_ANSWER'}
        remember_subject(turn, {'status': 'PARTIAL', 'deliveryStatus': 'FACT_SUMMARY'})
        restored = next_context_state(turn.state)
        selections = restored['inheritedScope']['selections']
        self.assertEqual([(s['role'], s['sourceRef']) for s in selections],
                         [('PRIMARY', primary['sourceRef']), ('REFERENCE', reference['sourceRef'])])
        following = TurnCompletion(self.service, {**restored, 'turnKey': 'next', 'request': '这些成员的其他指标'})
        directory_view = directory(following)
        self.assertIn('（主体） | 1 |', directory_view['markdown'])
        self.assertIn('（对照） | 2 |', directory_view['markdown'])

    async def test_direct_query_freezes_reviewed_result_instead_of_its_adopted_old_base(self):
        ref = self.prepare_sources()
        selected = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        old = register(self.service.turn, {**selected, 'rowIndices': [0]})
        await self.service.task(mode='DIRECT', goal='扩大范围后查询并排序',
                                subjectSources=[{'sourceRef': old['sourceRef']}])
        self.assertEqual(self.service.turn.state['sourceAdoptionTurnKey'], self.service.turn.state['turnKey'])
        capture, receipt = business_fixture()
        capture['request']['question'] = '扩大范围后的实际查询'
        receipt['data']['question'] = capture['request']['question']
        update(capture, receipt)
        fresh = self.store.capture(OWNER, capture)
        capture['request']['question'] = '之后的无关探索'
        receipt['data']['question'] = capture['request']['question']
        update(capture, receipt)
        exploration = self.store.capture(OWNER, capture)
        delivered = {**selected, 'captureRef': fresh}
        self.service.turn.state.update(queryCaptureRefs=[fresh, exploration],
            queryAnswerSubject={'selections': [delivered], 'basis': 'REVIEWED_ANSWER', 'candidateCount': 2})
        self.assertEqual(self.service.turn.state['adoptedInputs'][0]['rowCount'], 1)
        remember_subject(self.service.turn, {'status': 'DIRECT'})
        inputs = self.service.turn.state['adoptedInputs']
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]['rowCount'], 2)
        self.assertEqual(inputs[0]['selection']['captureRef'], fresh)
        restored = next_context_state(self.service.turn.state)
        self.assertNotIn('adoptedInputs', restored)
        self.assertEqual(restored['businessContexts'][-1]['adoptedInputs'], inputs)
        self.assertEqual(restored['inheritedScope']['selections'], [delivered])

    async def test_metadata_probe_does_not_replace_explicitly_adopted_facts(self):
        ref = self.prepare_sources()
        old = register(self.service.turn, {'captureRef': ref, 'stepId': 'batch',
            'batchId': 'batch-A', 'columnNames': ['metric1'], 'rowIndices': [0]})
        await self.service.task(mode='DIRECT', goal='说明已采用结果及可用定义',
                                subjectSources=[{'sourceRef': old['sourceRef']}])
        capture, _ = business_fixture()
        capture.update(originalToolName='get_context', request={},
                       response={'content': [{'type': 'text', 'text': '{"contextId":"context"}'}]})
        metadata = self.store.capture(OWNER, capture)
        self.service.turn.state['queryCaptureRefs'] = [metadata]
        remember_subject(self.service.turn, {'status': 'DIRECT'})
        self.assertEqual(self.service.turn.state['adoptedInputs'][0]['sourceRef'], old['sourceRef'])
        self.assertEqual(self.service.turn.state['resolvedSubjectScope']['selections'][0]['rowIndices'], [0])

    async def test_members_from_selects_exact_subset_without_retesting_value(self):
        ref = self.prepare_sources()
        selection = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        cohort = register(self.service.turn, {**selection, 'rowIndices': [0]})
        full = register(self.service.turn, {**selection, 'columnNames': ['metric2']})
        declared = await self.service.task(mode='ANALYSIS', goal='同一成员比较', materials=[{'role': 'PRIMARY', 'kinds': ['VALUES']}],
            subjectSources=[{'sourceRef': cohort['sourceRef']}, {'sourceRef': full['sourceRef'], 'membersFrom': cohort['sourceRef']}])
        self.assertEqual([item['rowCount'] for item in declared['subjectSources']], [1, 1])

    async def test_members_from_rejects_missing_entities_instead_of_silently_intersecting(self):
        ref = self.prepare_sources()
        selection = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A', 'columnNames': ['metric1']}
        cohort = register(self.service.turn, {**selection, 'rowIndices': [0]})
        other = register(self.service.turn, {**selection, 'rowIndices': [1]})
        with self.assertRaises(AnalysisError) as error:
            await self.service.task(mode='ANALYSIS', goal='同批对象', subjectSources=[{
                'sourceRef': other['sourceRef'], 'membersFrom': cohort['sourceRef']}])
        self.assertEqual(error.exception.code, 'COHORT_MEMBERS_MISSING')

    async def test_equal_entity_codes_in_different_value_domains_are_not_matched(self):
        capture, receipt = business_fixture()
        receipt['data']['stepResults'][0]['batchResults'][0]['metrics'][1]['dimensionModels']['MD_ORG'] = 'OTHER_ORGANIZATIONS'
        update(capture, receipt)
        ref = self.store.capture(OWNER, capture)
        self.service.turn = TurnCompletion(self.service, {'turnKey': 'domain', 'request': '同一批对象',
            'failures': {}, 'buildRefs': []})
        base = {'captureRef': ref, 'stepId': 'batch', 'batchId': 'batch-A'}
        first = register(self.service.turn, {**base, 'columnNames': ['metric1']})
        second = register(self.service.turn, {**base, 'columnNames': ['metric2']})
        with self.assertRaises(AnalysisError) as error:
            await self.service.task(mode='ANALYSIS', goal='比较', subjectSources=[{
                'sourceRef': second['sourceRef'], 'membersFrom': first['sourceRef']}])
        self.assertEqual(error.exception.code, 'COHORT_DOMAIN_MISMATCH')

    async def test_member_basis_excludes_metric_coordinates_and_fixed_context_columns(self):
        from analysis_extension.source_views import member_dimensions
        table = {'columnRoles': {dim: {'kind': 'DIMENSION'} for dim in ('MD_ORG', 'ACCOUNT', 'CURRENCY', 'VERSION')},
            'metrics': [{'name': 'profit', 'cellDimensions': {'ACCOUNT': 'net-profit'}}],
            'data': {'columns': [{'name': dim} for dim in ('MD_ORG', 'ACCOUNT', 'CURRENCY', 'VERSION')],
                     'rows': [['a', 'net-profit', 'CNY', 'submitted'], ['b', 'net-profit', 'CNY', 'submitted']]}}
        self.assertEqual(member_dimensions(table, ['profit'], {'data': {}}), ['MD_ORG'])
        table['data']['rows'] = table['data']['rows'][:1]
        proof = {'data': {'scopeResolution': [{'status': 'RESOLVED', 'dimCode': 'MD_ORG'}]}}
        self.assertEqual(member_dimensions(table, ['profit'], proof), ['MD_ORG'])
        table['columnRoles']['MD_SCENE'] = {'kind': 'DIMENSION'}
        self.assertEqual(member_dimensions(table, ['profit'], proof, ['MD_ORG', 'MD_SCENE', 'ACCOUNT']), ['MD_ORG'])
        with self.assertRaises(AnalysisError):
            member_dimensions(table, ['profit'], proof, ['MD_ORG', 'unknown-entity'])

    def test_property_group_cannot_be_reused_as_entity_members_or_silently_drop_grain(self):
        from analysis_extension.source_views import member_dimensions
        table = {'columnRoles': {'MD_ORG': {'kind': 'DIMENSION'}, 'SECTOR': {'kind': 'PROPERTY'}},
                 'metrics': [], 'data': {'columns': [], 'rows': []}}
        for grain in (None, ['MD_ORG'], ['MD_ORG', 'SECTOR']):
            with self.subTest(grain=grain), self.assertRaises(AnalysisError) as invalid:
                member_dimensions(table, [], {'data': {}}, grain)
            self.assertEqual(invalid.exception.code, 'COHORT_DETAIL_REQUIRED')
