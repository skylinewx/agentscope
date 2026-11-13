from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor

from analysis_business_query_test import business_fixture
from analysis_budget_batch_test import update
from analysis_extension.followup_scope import prepare_request
from analysis_extension.models import AnalysisError, Limits
from analysis_extension.store import AnalysisStore
from analysis_extension.adapters.budget_query import validate


class HierarchyFollowupTest(TestCase):
    def confirmed_turn(self):
        capture = {'originalToolName': 'execute_budget_query', 'response': {}, 'request': {
            'question': '查询一下久其集团26年的净利润预算', 'intentDraft': {
                'sharedConditions': [{'text': word, 'sourceText': word} for word in ('久其集团', '预算')],
                'targets': [{'id': 'profit', 'periodText': '26年', 'businessPhrases': [{'text': '净利润', 'sourceText': '净利润'}]}]}}}
        receipt = {'status': 'SUCCEEDED', 'data': {'resultSets': [{'resolvedTerms': [
            {'term': '久其集团', 'dimensionCode': 'MD_ORG'}, {'term': '预算', 'dimensionCode': 'MD_SCENE'},
            {'term': '净利润', 'dimensionCode': 'MD_BUDACCOUNT'}]}]}}
        service = SimpleNamespace(owner='owner', store=SimpleNamespace(get_capture=Mock(return_value=capture)),
            sources=SimpleNamespace(for_capture=Mock(return_value=SimpleNamespace(raw_receipt=Mock(return_value=receipt)))))
        state = self.state()
        state['inheritedScope'] = {'captureRefs': ['previous-query'], 'selections': []}
        return SimpleNamespace(service=service, state=state), capture

    def test_confirmed_capture_preserves_root_and_annual_period_when_view_changes(self):
        turn, _ = self.confirmed_turn()
        request = self.request()
        request['scope'] = {'periodCode': '2026Y0001'}
        request['intentDraft']['targets'] = [{'id': 'profit', 'periodText': '2026年1月', 'businessPhrases': []}]
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        self.assertIn('久其集团', result['contextText'])
        self.assertIn('26年', result['contextText'])
        self.assertEqual(result['scope']['periodCode'], '2026N0001')
        self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '26年')

    def test_explicit_value_switch_releases_only_its_old_value(self):
        for field, dimension, old, new, question in (
                ('population', 'MD_ORG', '久其集团', '其他集团', '换成其他集团'),
                ('scenario', 'MD_SCENE', '预算', '实际', '改查实际'),
                ('period', 'DATATIME', '26年', '25年', '再看25年')):
            turn, _ = self.confirmed_turn()
            turn.state['request'] = question
            turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedDimensions': [dimension]}
            request = {'purpose': 'QUERY', 'intentDraft': {'targets': [{'id': 'profit',
                'periodText': '25年' if field == 'period' else None, 'businessPhrases': []}]}}
            with patch('analysis_extension.followup_scope._same_source', return_value=True):
                result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
            self.assertNotIn(old, result.get('contextText', ''), field)
            self.assertIn('净利润', result['contextText'])
            if field != 'population': self.assertIn('久其集团', result['contextText'])
            if field != 'scenario': self.assertIn('预算', result['contextText'])
            if field != 'period': self.assertEqual(result['scope']['periodCode'], '2026N0001')
            else:
                self.assertNotIn('periodCode', result.get('scope', {}))
                self.assertEqual(result['intentDraft']['targets'][0]['periodText'], '25年')

    def test_followup_preserves_confirmed_compound_quote_without_model_retry(self):
        turn, capture = self.confirmed_turn()
        phrase = {'text': '预算', 'sourceText': '净利润预算', 'alternatives': [['净利润', '预算']]}
        capture['request']['intentDraft']['sharedConditions'] = [{'text': '久其集团', 'sourceText': '久其集团'}]
        capture['request']['intentDraft']['targets'][0]['businessPhrases'].append(phrase)
        request = self.request()
        request['intentDraft']['targets'] = deepcopy(capture['request']['intentDraft']['targets'])
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        for item in result['intentDraft']['targets'][0]['businessPhrases']:
            self.assertIn(item['sourceText'], result['question'] + ' ' + result['contextText'])
        self.assertIn('净利润预算', result['contextText'])

    def test_compound_quote_cannot_restore_explicitly_replaced_dimension_value(self):
        for dimension, quote, old in (('MD_SCENE', '净利润预算', '预算'),
                                      ('MD_ORG', '久其集团26年', '久其集团'),
                                      ('DATATIME', '26年', '26年')):
            turn, _ = self.confirmed_turn()
            turn.state['request'] = '替换条件'
            turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedDimensions': [dimension]}
            request = {'purpose': 'QUERY', 'intentDraft': {'targets': [{'id': 'profit',
                'businessPhrases': [{'text': '净利润', 'sourceText': quote}]}]}}
            with patch('analysis_extension.followup_scope._same_source', return_value=True):
                result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
            self.assertNotIn(old, result.get('contextText', ''), dimension)

    def test_public_phrase_without_source_text_reuses_exact_retained_historical_span(self):
        turn, _ = self.confirmed_turn()
        request = self.request()
        request['intentDraft']['targets'] = [{'id': 'profit', 'businessPhrases': [
            {'text': '净利润预算', 'alternatives': [['净利润', '预算']]}]}]
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        self.assertIn('净利润预算', result['contextText'])
        self.assertEqual(result['intentDraft']['targets'][0]['businessPhrases'][0]['sourceText'], '净利润预算')

    def test_public_compound_cannot_revive_changed_scenario_or_invent_quote(self):
        for text, changes in (('净利润预算', ['MD_SCENE']), ('预算净利润', [])):
            turn, _ = self.confirmed_turn()
            turn.state['request'] = '改查实际' if changes else '继续查询'
            turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedDimensions': changes}
            request = {'purpose': 'QUERY', 'intentDraft': {'targets': [{'id': 'profit',
                'businessPhrases': [{'text': text}]}]}}
            with patch('analysis_extension.followup_scope._same_source', return_value=True):
                with self.assertRaises(AnalysisError) as caught:
                    prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
            self.assertEqual(caught.exception.code, 'QUERY_DRAFT_EVIDENCE_INVALID')

    def test_quote_must_come_from_confirmed_source_not_concatenation_alone(self):
        turn, _ = self.confirmed_turn()
        request = self.request()
        request['intentDraft']['targets'] = [{'id': 'profit', 'businessPhrases': [
            {'text': '净利润', 'sourceText': '预算净利润'}]}]
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        self.assertNotIn('预算净利润', result['contextText'])

    def test_changed_metric_is_not_restored_from_a_misclassified_task_summary(self):
        turn, _ = self.confirmed_turn()
        turn.state['previousQueryIntent']['task'] = '净利润预算'
        turn.state['request'] = '水泥的销售预算'
        turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedFields': ['metric']}
        request = {'purpose': 'QUERY', 'intentDraft': {'targets': [{'id': 'sales',
            'businessPhrases': [{'text': '水泥', 'sourceText': '水泥'}]}]}}
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, request)
        self.assertNotIn('净利润', result['contextText'])
        self.assertIn('预算', result['contextText'])
        self.assertIn('久其集团', result['contextText'])
        self.assertEqual(result['scope']['periodCode'], '2026N0001')

    def test_explicit_task_name_is_still_retained_when_metric_changes(self):
        turn, capture = self.confirmed_turn()
        capture['request']['question'] += '，年度二上任务'
        capture['request']['scope'] = {'taskNames': ['年度二上']}
        turn.state['previousQueryIntent']['task'] = '年度二上'
        turn.state['followupContext'] = {'mode': 'CONTINUE', 'changedFields': ['metric']}
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, {'purpose': 'QUERY'})
        self.assertIn('年度二上', result['contextText'])

    def test_grouping_is_forwarded_without_reusing_legacy_filter_state(self):
        turn, _ = self.confirmed_turn()
        turn.state['queryIntent'] = {'grouping': '版块'}
        turn.state['budgetValueFilter'] = {'operator': 'LT', 'value': '0'}
        with patch('analysis_extension.followup_scope._same_source', return_value=True):
            result = prepare_request(turn, {'originalToolName': 'execute_budget_query'}, {'purpose': 'ANALYZE',
                'intentDraft': {'targets': [{'id': 'profit', 'businessPhrases': [{'text': '净利润'}]}]}})
        self.assertEqual(result['groupBy'], ['版块'])
        self.assertEqual(result['scope']['periodCode'], '2026N0001')
        self.assertNotIn('valueFilter', result['intentDraft'])  # legacy persisted field cannot leak into independent queries

    def state(self):
        return {'request': '从利润表进行分析，把二级单位的净利润做一个倒序排列',
                'previousQueryIntent': {'metric': '净利润', 'population': '久其集团', 'scenario': '预算', 'period': '2026年'},
                'queryIntent': {'population': '久其集团下属二级单位'},
                'followupContext': {'mode': 'CONTINUE', 'changedFields': ['population', 'grouping', 'order']}}

    def request(self, root='久其集团'):
        return {'purpose': 'ANALYZE', 'question': '不使用模型改写', 'contextText': '模型伪造上下文',
                'intentDraft': {'targets': [], 'relations': [{'type': 'HIERARCHY', 'targetIds': ['t'],
                    'sourceText': '二级单位', 'hierarchy': {'rootText': root, 'level': 2}}]}}

    def test_root_survives_population_change_only_for_declared_hierarchy(self):
        result = prepare_request(SimpleNamespace(state=self.state()), {'originalToolName': 'execute_budget_query'}, self.request())
        self.assertEqual(result['question'], self.state()['request'])
        self.assertIn('久其集团', result['contextText'])
        self.assertIn('2026年', result['contextText'])
        self.assertIn('预算', result['contextText'])
        self.assertNotIn('伪造', result['contextText'])

    def test_changing_root_or_starting_new_question_does_not_inherit_old_root(self):
        state = self.state()
        state['queryIntent']['population'] = '其他集团下属二级单位'
        state['request'] = '查询其他集团的二级单位净利润'
        result = prepare_request(SimpleNamespace(state=state), {'originalToolName': 'execute_budget_query'}, self.request('其他集团'))
        self.assertNotIn('久其集团', result.get('contextText', ''))
        state['followupContext']['mode'] = 'NEW'
        state['request'] = '查询久其集团的二级单位净利润'
        result = prepare_request(SimpleNamespace(state=state), {'originalToolName': 'execute_budget_query'}, self.request())
        self.assertNotIn('contextText', result)

    def test_hierarchy_fact_requires_native_selector_and_matching_row(self):
        capture, receipt = business_fixture()
        data = receipt['data']
        relation = {'rootText': '久其集团', 'level': 2}
        draft = {'relations': [{'type': 'HIERARCHY', 'targetIds': ['t0', 't1'], 'sourceText': '二级单位', 'hierarchy': relation}]}
        capture['request']['intentDraft'] = deepcopy(draft)
        data['resolution']['intentDraft'] = deepcopy(draft)
        root = {'term': '久其集团', 'baseDataCode': 'MD_ORG_BUD', 'objectCode': 'GT00', 'dimensionCode': 'MD_ORG'}
        for target, interpretation, result in zip(data['targets'], data['resolution']['interpretations'], data['resultSets']):
            target['scope'] = interpretation['scope'] = {'organizationHierarchy': relation}
            result['resolvedTerms'].append(root)
        selector = [{'dimCode': 'MD_ORG', 'hierarchy': {'rootCode': 'GT00', 'level': 2}}]
        table = data['stepResults'][0]['batchResults'][0]
        table['request']['batchDimensions'] = selector
        validate(capture, receipt)
        table['request']['batchDimensions'] = [{'dimCode': 'MD_ORG', 'values': ['GT00']}]
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        data['resultSets'][0]['rows'][0]['dimensions']['MD_ORG'] = table['data']['rows'][0][0]
        data['targets'][0]['scope']['organizationHierarchy']['level'] = 3
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)
        table['request']['batchDimensions'] = selector
        data['targets'][0]['scope']['organizationHierarchy']['level'] = 2
        data['resultSets'][0]['rows'][0]['dimensions']['MD_ORG'] = 'INVENTED'
        with self.assertRaises(AnalysisError):
            validate(capture, receipt)


class BlobQuotaTest(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = AnalysisStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, value):
        with self.store._transaction() as db:
            return self.store._put(db, {'value': value})

    def test_steady_writes_do_not_scan_blob_directory_and_concurrent_usage_is_exact(self):
        self.put('first')
        original = Path.iterdir
        def no_blob_scan(path):
            if path == self.store.blobs:
                raise AssertionError('per-write directory scan')
            return original(path)
        with patch.object(Path, 'iterdir', no_blob_scan), ThreadPoolExecutor(4) as pool:
            list(pool.map(self.put, range(20)))
        with self.store._transaction() as db:
            self.assertEqual(self.store._blob_usage(db), sum(p.stat().st_size for p in self.store.blobs.iterdir()))
        self.assertFalse(list(self.store.root.glob('blob-write-*.pending')))

    def test_rollback_orphans_are_counted_before_next_quota_check(self):
        self.put('first')
        with self.assertRaises(RuntimeError):
            with self.store._transaction() as db:
                self.store._put(db, {'orphan': 'x' * 100})
                raise RuntimeError('rollback after durable file')
        self.assertTrue(list(self.store.root.glob('blob-write-*.pending')))
        self.store = AnalysisStore(self.temp.name, Limits(max_store_bytes=120, max_receipt_bytes=120))
        with self.assertRaises(AnalysisError) as caught:
            self.put('another')
        self.assertEqual(caught.exception.code, 'STORE_FULL')

    def test_successful_write_recovers_crash_marker_once(self):
        self.put('first')
        (self.store.blobs / 'orphan-file').write_bytes(b'crash')
        (self.store.root / 'blob-write-dead.pending').write_bytes(b'crash')
        self.put('next')
        with self.store._transaction() as db:
            self.assertEqual(self.store._blob_usage(db), sum(p.stat().st_size for p in self.store.blobs.iterdir()))
        self.assertFalse(list(self.store.root.glob('blob-write-*.pending')))
