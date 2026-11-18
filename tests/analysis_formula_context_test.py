"""目标依赖闭包、数值断点及文档局部修正的通用回归。"""
from copy import deepcopy
import json

from analysis_closeout_test import mixed_fixture
from analysis_depth_test import chain
from analysis_fixtures import OWNER
from analysis_presentation_fixtures import PresentationCase
from analysis_extension.analysis_materials import materials
from analysis_extension.formulas import trace_formula
from analysis_extension.publication import repair_request
from analysis_extension.tools import AnalysisInspect
from jsonschema import Draft202012Validator
from analysis_extension.models import AnalysisError


class FormulaContextTests(PresentationCase):
    def test_explicit_selector_formula_check_is_not_automatically_duplicated(self):
        from analysis_extension.formula_inputs import add_checks
        facts = [{'factId': 'one', 'objectId': 'first', 'cellKey': '2:1'},
                 {'factId': 'two', 'objectId': 'second', 'cellKey': '2:1'}]
        operations = [{'id': 'explicit', 'operator': 'FORMULA_CHECK',
                       'select': {'objectId': 'first', 'cellKey': '2:1'}}]
        result = add_checks(operations, {'one', 'two'}, facts, {'members': ['first', 'second']})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], operations[0])
        self.assertEqual(result[1]['inputRefs'], ['two'])

    async def test_saved_minimal_publication_can_be_repaired_without_optional_arguments(self):
        reference, selectors, scope = mixed_fixture(self.store, 1)
        built = await self.service.build([reference], selectors, scope, [], 'saved-publication')
        source = self.service.delivery.root / 'repaired-page.html'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('<!doctype html><html><head><meta charset="utf-8"><title>修正页</title></head>'
                          '<body><p>来源值 {{value}}</p></body></html>', encoding='utf-8')
        artifact = self.store.get_analysis(OWNER, built['analysisRef'])
        saved = {'analysisRef': built['analysisRef'], 'document': {'title': '已保存的草案',
            'path': source.name, 'html': '冲突的旧内容',
            'bindings': [{'key': 'value', 'valueRef': artifact['facts'][0]['factId']}]}}
        proposal = self.store.save_publication_proposal(OWNER, built['analysisRef'], saved)
        preview = await self.service.publish(analysisRef=built['analysisRef'], repairRef=proposal,
            repairs=[{'op': 'remove', 'path': '/document/html'}], requestKey='repair-minimal', previewOnly=True)
        self.assertEqual(preview['summary']['document']['bindingCount'], 1)
        self.assertIn('confirmPublish', preview)

    async def test_missing_formula_operand_does_not_block_unrelated_python_statistics(self):
        old_ref, selectors, scope = mixed_fixture(self.store, 1)
        capture = self.store.get_capture(OWNER, old_ref)
        receipt = json.loads(capture['response']['content'][0]['text'])
        receipt['data']['stepResults'][1]['formResults'][0]['page']['cells'][1].update(
            rawValueJson='null', displayValue='')
        capture['response']['content'][0]['text'] = json.dumps(receipt)
        reference = self.store.capture(OWNER, capture)
        selectors[0]['captureRef'] = scope['population']['captureRef'] = reference
        result = await self.service.python(captureRefs=[reference], selections=selectors, scope=scope,
            operations=[{'id': 'missing', 'operator': 'FORMULA_CHECK',
                         'select': {'cellKey': '2:1', 'objectId': 'object-0'}}],
            code='result=[{"name":"count","label":"所选事实数","value":len(inputs),"unit":"项","inputRefs":[x["ref"] for x in inputs]}]',
            purpose='保留缺数诊断并统计已选事实', requestKey='missing-operand')
        self.assertEqual(result['derivedValues'][-1]['value'], '3')
        self.assertEqual(result['excludedOperationInputs'][0]['ref'], 'derived_missing')
        artifact = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertNotEqual(artifact['derivedValues'][0]['calculationValidation'], 'PASS')
        replayed = await self.service.python(captureRefs=[reference], selections=selectors, scope=scope,
            operations=[{'id': 'missing', 'operator': 'FORMULA_CHECK',
                         'select': {'cellKey': '2:1', 'objectId': 'object-0'}}],
            code='result=[{"name":"count","label":"所选事实数","value":len(inputs),"unit":"项","inputRefs":[x["ref"] for x in inputs]}]',
            purpose='保留缺数诊断并统计已选事实', requestKey='missing-operand')
        self.assertEqual(replayed['excludedOperationInputs'], result['excludedOperationInputs'])
        with self.assertRaises(AnalysisError) as caught:
            await self.service.python(analysisRef=result['analysisRef'], inputRefs=['derived_missing'],
                code='result=[]', purpose='明确使用未完成计算应报错', requestKey='explicit-missing')
        self.assertEqual(caught.exception.code, 'PYTHON_INPUT_UNVERIFIED')
        self.assertEqual(caught.exception.details['ref'], 'derived_missing')
        self.assertFalse(caught.exception.details['requiresSourceQuery'])

    async def test_batch_reads_inherit_only_explicit_analysis_identity(self):
        reference, selectors, scope = mixed_fixture(self.store, 1)
        built = await self.service.build([reference], selectors, scope, [], 'shared-read')
        request = {'analysisRef': built['analysisRef'], 'requests': [{'section': 'facts'}, {'section': 'summary'}]}
        Draft202012Validator(AnalysisInspect.input_schema).validate(request)
        result = await self.service.inspect(**request)
        self.assertEqual(len(result['results']), 2)
        self.assertTrue(all(item['status'] != 'ERROR' for item in result['results']))

    async def test_targeted_formula_context_builds_checks_without_manual_dependency_selection(self):
        old_ref, _, scope = mixed_fixture(self.store, 2)
        captured = self.store.get_capture(OWNER, old_ref)
        receipt = json.loads(captured['response']['content'][0]['text'])
        for index, step in enumerate(receipt['data']['stepResults'][1:]):
            form = step['formResults'][0]
            cells = form['page']['cells'][:3]
            for position, cell in enumerate(cells):
                cell['formulaStructure'] = {'address': 'ABC'[position] + '1',
                    'expression': cell['formula'], 'references': ['B1', 'C1'] if position == 0 else [],
                    'status': 'PARSED' if position == 0 else 'SOURCE_VALUE', 'issue': None}
            form.update(status='VALUE_FOUND', cells=cells, page=None,
                        formulaGraph={'targetKeys': ['2:1'], 'cellKeys': ['2:1', '2:2', '2:3'], 'complete': True, 'issues': []})
            captured['request']['plan']['steps'][index + 1]['formQuery'].update(
                includeFormulaDependencies=True, targetDimensions={'ACCOUNT': '结果'})
        captured['response']['content'][0]['text'] = json.dumps(receipt, ensure_ascii=False)
        reference = self.store.capture(OWNER, captured)
        scope['population']['captureRef'] = reference
        selected = materials(self.store.get_capture(OWNER, reference), self.service.sources)['selections']
        self.assertEqual([item['cellKeys'] for item in selected], [['2:1'], ['2:1']])
        self.assertTrue(all(item['includeFormulaDependencies'] for item in selected))
        result = await self.service.build([reference], selected, scope, [], 'targeted-formula')
        stored = self.store.get_analysis(OWNER, result['analysisRef'])
        self.assertEqual(len(stored['facts']), 6)
        self.assertEqual(len(stored['derivedValues']), 2)
        self.assertTrue(all(item['calculationValidation'] == 'PASS' for item in stored['derivedValues']))
        self.assertEqual({item['value'] for item in stored['derivedValues']}, {'-5', '20'})
        for item in stored['derivedValues']:
            objects = {fact['objectId'] for fact in stored['facts'] if fact['factId'] in item['sourceFactIds']}
            self.assertEqual(len(objects), 1)

    def test_null_node_does_not_hide_captured_grandchild_formula(self):
        facts = chain()
        facts[1].update(value=None, valueState='NO_VALUE')
        result = trace_formula(facts, 'root')
        self.assertTrue(result['structureComplete'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual({node['factRef'] for node in result['nodes']}, {fact['factId'] for fact in facts})
        subtotal = next(node for node in result['nodes'] if node['factRef'] == 'subtotal')
        self.assertEqual(subtotal['recalculated'], '107')
        self.assertIsNone(subtotal['difference'])

    def test_document_replacement_preserves_bindings_unless_explicitly_changed(self):
        saved = {'analysisRef': 'analysis', 'document': {'path': 'old.html', 'title': '原页',
                 'bindings': [{'key': 'value', 'valueRef': 'fact'}], 'datasets': [{'key': 'chart', 'valueRef': 'derived'}]}}
        original = deepcopy(saved)
        repaired = repair_request(saved, 'analysis', [{'op': 'replace', 'path': '/document',
                                   'value': {'path': 'new.html', 'title': '更新页'}}])
        self.assertEqual(repaired['document']['bindings'], saved['document']['bindings'])
        self.assertEqual(repaired['document']['datasets'], saved['document']['datasets'])
        self.assertEqual(saved, original)
        cleared = repair_request(saved, 'analysis', [{'op': 'replace', 'path': '/document',
                                  'value': {'path': 'new.html', 'title': '更新页', 'bindings': [], 'datasets': []}}])
        self.assertEqual(cleared['document']['bindings'], [])
        self.assertEqual(cleared['document']['datasets'], [])
