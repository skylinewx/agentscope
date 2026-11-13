from copy import deepcopy
from types import SimpleNamespace
import unittest

from analysis_extension.build_projection import project, render
from analysis_extension.query_recovery import target_recovery
from analysis_extension.grouping_policy import remember_grouping
from analysis_horizontal_waterfall_test import waterfall_artifact


class StabilityHandoffTests(unittest.TestCase):
    def test_publication_handoff_preserves_errors_and_repair_without_duplicate_evidence(self):
        from analysis_extension.publication_projection import project as publish_project
        checks = {'status': 'ERRORS', 'viewports': [{'viewport': {'width': 420},
            'errors': ['bad binding', 'bad binding'], 'orders': [],
            'valueCoverage': {'complete': False, 'sourceRefs': ['a', 'b'], 'presented': ['a']}}]}
        value = {'analysisRef': 'test:1', 'repairRef': 'proposal',
                 'diagnostics': {'renderChecks': checks,
                    'publicationCheck': {'code': 'DOCUMENT_RENDER_ERRORS', 'details': {'renderChecks': deepcopy(checks)}}},
                 'repairAndFinalize': {'analysisRef': 'test:1', 'repairRef': 'proposal'}}
        before = deepcopy(value)
        projected = publish_project(value)
        self.assertEqual(value, before)
        viewport = projected['diagnostics']['renderChecks']['viewports'][0]
        self.assertEqual(viewport['errors'], ['bad binding'])
        self.assertEqual(viewport['errorsOccurrences'], [2])
        self.assertFalse(viewport['valueCoverage']['complete'])
        self.assertEqual(viewport['valueCoverage']['sourceRefsCount'], 2)
        self.assertEqual(projected['repairAndFinalize'], value['repairAndFinalize'])
        self.assertEqual(projected['savedDiagnosticsRead']['previewRef'], 'proposal')

    def test_adopted_sources_hide_duplicate_build_provenance_from_model_only(self):
        from analysis_extension.tools import AnalysisBuild
        service = SimpleNamespace(turn=SimpleNamespace(state={}))
        tool = AnalysisBuild(service)
        self.assertIsNone(tool.model_input_schema)
        service.turn.state['adoptedInputs'] = [{'inputRef': 'chosen'}]
        self.assertNotIn('selections', tool.model_input_schema['properties'])
        self.assertNotIn('captureRefs', tool.model_input_schema['properties'])
        self.assertIn('operations', tool.model_input_schema['properties'])
        self.assertIn('selections', tool.input_schema['properties'])
        service.turn.state['adoptedInputs'] = []
        self.assertIsNone(tool.model_input_schema)

    def test_source_only_closeout_does_not_count_retries_as_new_group_members(self):
        from analysis_extension.model_closeout import query_closeout_facts
        def source(ref, value='10', form='表甲', snapshot='same'):
            return {'captureRef': ref, 'sourceConnection': {'resourceId': 'budget'},
                    'source': {'context': {'sourceId': 'source', 'snapshotId': snapshot}},
                    'contexts': {'c': {'taskName': '任务甲', 'formName': form, 'period': '2026N0001'}},
                    'facts': [{'factId': ref + '-fact', 'contextRef': 'c', 'objectLabel': '板块甲',
                               'dimensions': {'MD_ORG__sector': 'A'}, 'value': value, 'valueState': 'PRESENT'}]}
        sources = [source('first'), source('retry'), source('changed', '11'), source('other-form', form='表乙'), source('other-snapshot', snapshot='other')]
        before = deepcopy(sources)
        facts = query_closeout_facts(sources)
        self.assertEqual(len(facts), 4)
        self.assertEqual([o['captureRef'] for o in facts[0]['observations']], ['first', 'retry'])
        self.assertEqual(sources, before)

    def test_readable_build_preserves_formula_amounts_dependencies_and_scope_gaps(self):
        artifact = waterfall_artifact()
        artifact['analysisRef'] = 'test:1'
        value = {'analysisRef': 'test:1', 'scope': {'members': ['A'], 'referenceMembers': ['A', 'B'],
            'pendingMembers': ['C'], 'excludedMembers': ['D'], 'populationComplete': False,
            'inputGroups': [{'inputRef': 'primary', 'role': 'PRIMARY', 'factRefs': ['root'], 'members': ['A']},
                            {'inputRef': 'reference', 'role': 'REFERENCE', 'members': ['A', 'B']}]},
            'derivedValues': deepcopy(artifact['derivedValues']), 'documentInputs': [{'valueRef': 'derived_formula', 'kind': 'FORMULA'}],
            'verificationTasks': [{'executionId': 'host-proof'}], 'compositionMaterials': {'unused': 'duplicate'}}
        before = deepcopy(value), deepcopy(artifact)
        projected = project(value, artifact)
        self.assertEqual((value, artifact), before)
        self.assertEqual(projected['scope']['pendingMembers'], ['C'])
        self.assertEqual(projected['scope']['referenceMembersCount'], 2)
        self.assertEqual(projected['scope']['inputGroups'][1]['role'], 'REFERENCE')
        view = projected['formulaMaterials'][0]
        self.assertTrue(view['readComplete'])
        self.assertEqual(view['references'][0]['sourceExpression'], 'B1-C1+2')
        self.assertEqual({d['factRef'] for d in view['references'][0]['dependencies']}, {'subtotal', 'expense'})
        self.assertIn('差额=-4', render(value, artifact))
        self.assertEqual(projected['documentInputs'], value['documentInputs'])

    def test_target_recovery_retains_original_conditions_without_reinterpreting_null(self):
        request = {'contractVersion': '2.0', 'contextId': 'context', 'querySessionId': 'query',
                   'intentDraft': {'targets': [{'businessPhrases': [{'text': '培训'}]}]},
                   'scope': {'periodCode': '2026N0001'}}
        before = deepcopy(request)
        recovery = target_recovery({'status': 'FAILED', 'error': {'code': 'BUDGET_TARGET_UNRESOLVED'}}, request)
        self.assertEqual(recovery['sourceTerms'], ['培训'])
        self.assertEqual(recovery['searchArguments']['contextId'], 'context')
        self.assertEqual(request, before)
        for receipt in ({'status': 'SUCCEEDED', 'data': {'failureSummary': {'outcome': 'NO_DATA'}}},
                        {'status': 'FAILED', 'error': {'code': 'SOURCE_TIMEOUT'}}):
            self.assertIsNone(target_recovery(receipt, request))

    def test_historical_string_grouping_is_one_requirement_not_characters(self):
        receipt = {'status': 'FAILED', 'data': {'grouping': []}}
        adapter = SimpleNamespace(adapter_id='budget-v2', raw_receipt=lambda response: receipt)
        service = SimpleNamespace(sources=SimpleNamespace(for_capture=lambda capture: adapter))
        for raw in ('板块', '["板块"]', ['板块']):
            turn = SimpleNamespace(service=service, state={})
            capture = {'captureRef': 'cap', 'originalToolName': 'execute_budget_query',
                       'request': {'groupBy': raw}, 'response': {}, 'resourceId': 'budget',
                       'serverName': 'server', 'connectionFingerprint': 'same'}
            remember_grouping(turn, capture)
            self.assertEqual([r['keyword'] for r in turn.state['groupingRequirements'].values()], ['板块'])
