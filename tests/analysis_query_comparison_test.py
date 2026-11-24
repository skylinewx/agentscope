from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from analysis_business_tables_test import fact
from analysis_markdown_delivery_test import query_turn
from analysis_extension.direct_query_delivery import source_answer
from analysis_extension.models import AnalysisError
from analysis_extension.query_comparison import make_plan, requests


class ComparisonTests(TestCase):
    def test_split_queries_send_only_literals_exclusive_to_a_different_registered_lookup(self):
        from analysis_extension.query_comparison import batch_coverage
        turn = SimpleNamespace(state={'request': '比较2026年材料甲维修费预算和去年同期实际数'})
        plan = make_plan(turn, {'shared': ['材料甲'], 'items': [
            {'terms': ['维修费'], 'periodText': '2026年', 'scenario': '预算'},
            {'terms': ['维修费'], 'periodText': '去年同期', 'scenario': '实际数',
             'periodRequest': {'kind': 'PERIOD', 'start': {'type': 'YEAR', 'year': 2025}}}]})
        turn.state['comparisonPlan'] = plan
        (current, _), (prior, request) = list(requests(plan))
        self.assertEqual(batch_coverage(turn.state, request), {'targetIds': prior, 'peers': [
            {'targetId': current[0], 'terms': [], 'scenario': '预算', 'periodText': '2026年'}]})
        self.assertIsNone(batch_coverage(turn.state, {'intentDraft': {'targets': [{'id': 'unrelated'}]}}))
        self.assertIsNone(batch_coverage(turn.state, {'intentDraft': {'targets': [{'id': i} for i in current+prior]}}))
        for caption in ('2025年', '去年同期'):
            with self.subTest(caption=caption), self.assertRaisesRegex(AnalysisError, 'periodRequest'):
                make_plan(turn, {'items': [{'terms': ['维修费'], 'periodText': '2026年'},
                                            {'terms': ['维修费'], 'periodText': caption}]})

    def test_source_pair_preserves_new_shared_filters_and_wraps_one_period_point(self):
        turn = SimpleNamespace(state={'request': '把刚才两项与去年同期比较'}, service=SimpleNamespace(owner='o',
            store=SimpleNamespace(get_source_view=lambda *_: {'selection':{'captureRef':'c','resultSetId':'r'},'rowCount':2},
                                  get_capture=lambda *_: {})))
        data = {'data': {'resultSets':[{'resultSetId':'r','context':{'formName':'采购明细'},
            'resolvedTerms':[{'dimensionCode':'物料','term':'矿石'}], 'rows':[
                {'dataPeriod':'2024N0001','dimensionNames':{'组织':'集团','部门':'供应'}},
                {'dataPeriod':'2024N0001','dimensionNames':{'组织':'集团','部门':'采购'}}]}]}}
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=data), patch(
                'analysis_extension.history_references.bind', return_value=[{'sourceRef':'s','contextRef':'ctx'}]):
            plan = make_plan(turn, {'shared':['集团','矿石','供应','零售'], 'items':[
                {'sourceRef':'s'}, {'sameAs':1,'mention':'刚才两项','periodText':'去年同期',
                                     'periodRequest':{'type':'YEAR','yearOffset':-1}}]})
        self.assertEqual(plan['items'][0]['contextRef'], 'ctx')
        _, query = list(requests(plan))[1]
        self.assertEqual(query['scope']['periodRequest'], {'kind':'PERIOD','start':{'type':'YEAR','yearOffset':-1},
                                                          'referencePeriod':'2024N0001'})
        self.assertEqual(query['intentDraft']['sharedConditions'], [{'text':'供应'}, {'text':'零售'}])

    def test_source_pairs_bind_the_selected_form_and_relative_period(self):
        from analysis_extension.query_comparison import _requery_conditions
        source = {'selection': {'captureRef': 'cap', 'resultSetId': 's'}, 'rowCount': 1}
        turn = SimpleNamespace(service=SimpleNamespace(owner='o', store=SimpleNamespace(
            get_source_view=lambda owner, ref: source, get_capture=lambda owner, ref: {})), state={'request':'与上年同期比较'})
        native = {'data': {'resultSets': [{'resultSetId':'s', 'context': {'formName': '费用明细'},
            'resolvedTerms': [{'dimensionCode':'MD_EXP','term':'服务费'}, {'dimensionCode':'MD_SCENE','term':'预算'}],
            'rows':[{'dataPeriod':'2024J0003'}]}]}}
        period = {'kind':'PERIOD','start':{'type':'QUARTER','yearOffset':-1}}
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=native), patch(
                'analysis_extension.history_references.bind', return_value=[{'sourceRef':'archive'}]):
            plan = make_plan(turn, {'items': [{'sourceRef':'archive'}, {'sameAs':1,'periodText':'上年同期',
                                              'periodRequest':period,'scenario':'实际数'}]})
            new = list(requests(plan))[1][1]
            self.assertEqual(new['basis'], {'kind':'MEMBERS','sourceRef':'archive'})
            self.assertEqual(new['scope']['periodRequest']['referencePeriod'], '2024J0003')
            self.assertEqual(new['scope']['formNames'], ['费用明细'])
            self.assertEqual(new['intentDraft']['targets'][0]['businessPhrases'], [{'text':'服务费'}])
            self.assertEqual(new['intentDraft']['sharedConditions'], [{'text':'实际数'}])
            native['data']['resultSets'][0]['rows'].append({'dataPeriod':'2024J0004'})
            with self.assertRaisesRegex(AnalysisError, '多个数据时期'):
                _requery_conditions(turn, 'archive', {'periodRequest':period})

    def test_compiles_only_declared_groups_and_target_owned_dimensions(self):
        plan = make_plan(None, {'shared': ['材料'], 'items': [
            {'terms': ['收入'], 'dimensions': {'渠道': ['零售']}},
            {'terms': ['费用']}, {'terms': ['销量'], 'form': '销售表'}], 'groups': [[1, 2], [1, 3]]})
        (ids, first), (last, second) = list(requests(plan))
        self.assertEqual(first['intentDraft']['relations'], [{'type': 'COMPARE', 'targetIds': ids}])
        self.assertEqual(first['intentDraft']['dimensionScopes'][0]['targetIds'], ids[:1])
        self.assertEqual(second['scope'], {'formNames': ['销售表']})
        self.assertEqual(second['intentDraft']['relations'], [])
        self.assertEqual(plan['groups'], [ids, [ids[0], last[0]]])
        with self.assertRaises(AnalysisError):
            make_plan(None, {'items': [{'terms': [str(i)]} for i in range(3)], 'groups': [[1, 2]]})

    def test_frozen_and_new_reads_keep_their_own_basis(self):
        with patch('analysis_extension.history_references.bind', return_value=[{'sourceRef': 'source-a'}]):
            plan = make_plan(None, {'items': [{'mention': '刚才'}, {'terms': ['供货量']}]})
        frozen, fresh = list(requests(plan))
        self.assertEqual(frozen[1]['basis'], {'kind': 'FACTS', 'sourceRef': 'source-a'})
        self.assertEqual(fresh[1]['basis'], {'kind': 'INTENT'})
        self.assertNotIn('intentDraft', frozen[1])

    def test_literal_mention_on_new_items_keeps_the_same_lookup(self):
        turn = SimpleNamespace(state={'request': '比较零售销量与批发销量'})
        items = [{'terms': ['零售'], 'mention': '零售销量'}, {'terms': ['批发']}]
        plan = make_plan(turn, {'items': items})
        self.assertEqual(plan, make_plan(turn, {'items': [{'terms': ['零售']}, {'terms': ['批发']}]}))
        with self.assertRaises(AnalysisError):
            make_plan(turn, {'items': [{**items[0], 'mention': '批发销量'}, items[1]]})
        with self.assertRaises(AnalysisError):
            make_plan(turn, {'items': [{**items[0], 'sourceRef': 'archive'}, items[1]]})

    def test_independent_receipts_mixed_with_empty_duplicate_and_foreign_receipts(self):
        turn, data = query_turn([fact('f1', {}, '1250.00')])
        turn.state['turnKey'] = 'current'
        plan = make_plan(turn, {'items': [{'terms': ['甲项']}, {'terms': ['乙项'], 'form': '表乙'}]})
        a, b = (item['id'] for item in plan['items'])
        first = turn.service.store.get_capture(None, 'c')
        first.update(turnKey='current', captureRef='a', connectionFingerprint='conn')
        first['response']['data']['targets'][0]['logicalTargetId'] = a
        second = deepcopy(first)
        second.update(captureRef='b')
        second['response']['data']['targets'][0].update(logicalTargetId=b, label='另一指标')
        second['response']['data']['resultSets'][0]['rows'] = []
        captures = {'a': first, 'b': second, 'other': dict(second, turnKey='older')}
        turn.service.store = SimpleNamespace(get_capture=lambda _, ref: captures[ref])
        plan['runs'] = [{'items': [a], 'refs': ['a', 'a']}, {'items': [b], 'refs': ['other']}]
        turn.state.update(queryTargets=[a, b], queryCaptureRefs=['a','b','other'], comparisonPlan=plan)
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', side_effect=lambda c: c['response']):
            self.assertIsNone(source_answer(turn))
            partial = source_answer(turn, at_reply_end=True)
            self.assertTrue(partial['incomplete'])
            self.assertIn('乙项', partial['answer'])
            plan['runs'][1]['refs'] = ['b']
            answer = source_answer(turn)
            self.assertIsNone(answer['emptyStatus'])
            self.assertEqual(answer['refs'], ['a','b'])
            self.assertIn('1,250.00', answer['answer'])
            self.assertIn('无数据', answer['answer'])
            self.assertIn('1,250.00', answer['material'])
            self.assertEqual(answer['answer'].count('**查询条件**'), 1)
            first['response']['data']['resultSets'][0]['rows'] = []
            self.assertEqual(source_answer(turn)['emptyStatus'], 'NO_DATA')

    def test_partial_receipt_preserves_only_proven_independent_items(self):
        turn, _ = query_turn([fact('f1', {}, '15.00')])
        plan = make_plan(turn, {'items': [{'terms':['服务费']}, {'terms':['运费']}]})
        a,b = [i['id'] for i in plan['items']]
        cap = turn.service.store.get_capture(None,'c')
        cap.update(turnKey='turn', captureRef='c')
        raw = cap['response']
        raw['status'] = 'PARTIAL'
        data = raw['data']
        data.update(complete=False, unresolvedGroups=[{'code':'TARGET_DISCOVERY_UNRESOLVED','diagnostics':{'logicalTargetId':b}}])
        data['targets'][0]['logicalTargetId'] = a
        plan['runs'] = [{'items':[a,b], 'refs':['c']}]
        turn.state.update(turnKey='turn', comparisonPlan=plan)
        with patch('analysis_extension.adapters.budget_v2.receipt_from_capture', return_value=raw):
            self.assertIsNone(source_answer(turn))
            result = source_answer(turn, at_reply_end=True)
            self.assertTrue(result['incomplete'])
            self.assertIn('15.00', result['answer'])
            self.assertIn('运费',result['answer'])
            data['unresolvedGroups'][0]['code'] = 'BATCH_QUERY_FAILED'
            self.assertNotIn('15.00',source_answer(turn, at_reply_end=True)['answer'])
