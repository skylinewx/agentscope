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
    def prepare_sources(self):
        capture, _ = business_fixture()
        ref = self.store.capture(OWNER, capture)
        state = {'turnKey': 'sources', 'request': '分别分析两个结果', 'failures': {}, 'buildRefs': [],
                 'queryCaptureRefs': [ref], 'inheritedScope': {}}
        self.service.turn = TurnCompletion(self.service, state)
        return ref

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
        declared = await self.service.task(mode='ANALYSIS', goal='分别分析两个结果', requiredEvidence='VALUES',
            subjectSources=[{'sourceRef': item['sourceRef'], 'role': 'PRIMARY'} for item in sources])
        inputs = declared['subjectSources']
        built = await self.service.build(operations=[{'id': 'count' + str(i), 'operator': 'COUNT',
            'select': {'inputRef': item['inputRef']}} for i, item in enumerate(inputs)], requestKey='two-primary')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        self.assertEqual(len(artifact['scope']['inputGroups']), 2)
        self.assertTrue(all(value['calculationValidation'] == 'PASS' for value in artifact['derivedValues']))
        remember_subject(self.service.turn, {'status': 'READY', 'analysisRef': built['analysisRef']})
        restored = next_context_state(deepcopy(self.service.turn.state))
        self.assertEqual(len(restored['adoptedInputs']), 2)

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
        declared = await self.service.task(mode='ANALYSIS', goal='主体和对照分别统计', requiredEvidence='VALUES', subjectSources=choices)
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
        with self.assertRaisesRegex(AnalysisError, '另一组输入'):
            validate_analysis(self.service, artifact)

    async def test_rewording_goal_and_reordering_sources_keeps_data_version_and_preparation(self):
        self.prepare_sources()
        choices = [{'sourceRef': item['sourceRef']} for item in directory(self.service.turn)['sources']]
        await self.service.task(mode='ANALYSIS', goal='分析这两份结果', requiredEvidence='VALUES', subjectSources=choices)
        built = await self.service.build(operations=[], requestKey='unchanged-inputs')
        version = self.service.turn.state['inputSetVersion']
        self.service.turn.state['analysisRef'] = built['analysisRef']
        self.service.turn.state['budgetFormulaPreparation'] = {'kept': 'same data'}
        await self.service.task(mode='ANALYSIS', goal='换个角度比较同一份结果', requiredEvidence='VALUES',
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
        await self.service.task(mode='ANALYSIS', goal='原目标', requiredEvidence='VALUES', subjectSources=choices)
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
        await self.service.task(mode='ANALYSIS', goal='新的解释目标', requiredEvidence='VALUES', subjectSources=choices)
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
        self.assertEqual(restored['adoptedInputs'], inputs)
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
        declared = await self.service.task(mode='ANALYSIS', goal='同一成员比较', requiredEvidence='VALUES',
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
