"""丰富交付、协议字段和同轮续建的集成回归，不以适配函数单测代替持久化验证。"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest import TestCase

from analysis_fixtures import OWNER, saved_fixture
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.models import AnalysisError
from analysis_extension.protocol import validate_artifact
from analysis_extension.turn_completion import TurnCompletion, semantic_review_key


class RichDeliveryTests(PresentationCase):
    def task(self):
        return TurnCompletion(self.service, {'mode': None, 'buildRefs': [], 'reviews': [], 'declaredGoals': []})

    def test_analysis_defaults_to_document_and_rejects_text(self):
        turn = self.task()
        self.assertEqual(turn.task(mode='ANALYSIS', goal='查询指标构成')['delivery'], 'DOCUMENT')
        with self.assertRaises(AnalysisError):
            turn.task(mode='ANALYSIS', goal='构成查询', delivery='TEXT')

    def test_plain_query_is_light_and_explicit_rich_query_requires_publication(self):
        plain = self.task()
        self.assertEqual(plain.task(mode='DIRECT', goal='查询一个值')['delivery'], 'TEXT')
        self.assertEqual(plain.check()['status'], 'DIRECT')
        rich = self.task()
        rich.task(mode='DIRECT', goal='展示分布构成', delivery='DOCUMENT')
        self.assertEqual(rich.check()['status'], 'INCOMPLETE')
        self.assertEqual(rich.check()['issues'][0]['code'], 'ANALYSIS_NOT_BUILT')

    async def test_typed_composite_input_builds_and_round_trips_with_empty_exclusions(self):
        capture, selectors, _ = saved_fixture(self.store, ('100', '-3'))
        scope = {'label': '两家单位', 'selectionReason': '按用户范围分析'}
        typed = [{**selector, 'role': 'PRIMARY', 'grain': ['MD_ORG', 'DATATIME']} for selector in selectors]
        result = await self.service.build([capture], typed, scope, [], 'typed')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(artifact['scope']['excludedMembers'], [])
        self.assertEqual(len(artifact['scope']['members']), 2)
        self.assertEqual([fact['value'] for fact in artifact['facts']], ['100', '-3'])
        validate_artifact(artifact)
        broken = deepcopy(artifact)
        del broken['scope']['excludedMembers']
        with self.assertRaises(AnalysisError) as caught:
            validate_artifact(broken)
        self.assertEqual(caught.exception.details['issues'][0],
                         {'path': '/scope', 'constraint': 'required', 'missingFields': ['excludedMembers']})
        self.assertFalse(caught.exception.details['requiresSourceQuery'])

    async def test_implicit_continuation_keeps_existing_inputs_and_work_refs(self):
        capture, selectors, _ = saved_fixture(self.store, ('10', '20'))
        selectors = [{**selector, 'role': 'PRIMARY'} for selector in selectors]
        scope = {'label': '逐步补齐单位', 'selectionReason': '用户要求两个单位', 'memberDimension': 'MD_ORG'}
        first = await self.service.build([capture], selectors[:1], scope, [], 'first',
                                         workPlan=[{'taskId': 'values', 'title': '取得所选数据', 'kind': 'VALUES', 'refs': []}])
        self.service.turn = SimpleNamespace(state={'analysisRef': first['analysisRef']})
        second = await self.service.build([capture], selectors[1:], scope, [], 'append')
        artifact = self.store.get_analysis(OWNER, second['analysisRef'])
        self.assertEqual([fact['value'] for fact in artifact['facts']], ['10', '20'])
        self.assertEqual(artifact['plan']['workPlan'][0]['taskId'], 'values')
        self.assertEqual(len(artifact['plan']['selections']), 2)


class SemanticReviewReuseTests(TestCase):
    def test_publication_only_changes_reuse_but_new_business_text_or_values_do_not(self):
        material = {'goal': '解释构成', 'facts': [{'factId': 'f', 'value': '10'}],
                    'documentText': '构成说明', 'candidateAnswer': '结论',
                    'deliveryEvidence': {'analysisRef': 'a:1'}, 'reviewHistory': []}
        updated = deepcopy(material)
        updated['deliveryEvidence']['analysisRef'] = 'a:2'
        updated['reviewHistory'] = [{'analysisRef': 'a:1', 'issues': []}]
        self.assertEqual(semantic_review_key(material, 'review'), semantic_review_key(updated, 'review'))
        for key, value in [('documentText', '新的结论'), ('facts', [{'factId': 'f', 'value': '20'}]), ('goal', '分析全部单位')]:
            changed = {**updated, key: value}
            self.assertNotEqual(semantic_review_key(material, 'review'), semantic_review_key(changed, 'review'))
