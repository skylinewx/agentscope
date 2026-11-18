"""本层计算成功、逐层缺数时，报告不能把不可用差额渲染为空白。"""
from copy import deepcopy
import json
import re
from unittest import TestCase

from jsonschema import Draft202012Validator

from analysis_depth_test import chain
from analysis_extension.engine import RuleRegistry, calculate
from analysis_extension.free_document import compile_document
from analysis_extension.models import AnalysisError
from analysis_extension.tools import DOCUMENT_BINDING
from analysis_extension.validation import executable_field, render_parts, selected_value


class FormulaBindingTests(TestCase):
    def test_default_scope_is_local_and_deeper_gaps_do_not_block_it(self):
        facts = chain()
        facts[4].update(value=None, valueState='NO_VALUE')
        result = calculate(facts, {'scopeId': 'scope', 'coverage': 'UNKNOWN'}, [
            {'id': 'current', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root']}], RuleRegistry())[0]
        self.assertEqual(result['checkScope'], 'LOCAL')
        self.assertEqual(result['calculationValidation'], 'PASS')
        self.assertEqual(result['formulaTrace']['status'], 'BLOCKED')

    def artifact(self, check_scope='LOCAL', missing=True):
        facts = chain()
        facts[0]['value'] = '-8300.00'
        facts[1]['value'] = '2860480.43'
        facts[2]['value'] = '1545800.00'
        if missing:
            facts[4].update(value=None, valueState='NO_VALUE')
        scope = {'scopeId': 'scope', 'coverage': 'UNKNOWN'}
        derived = calculate(facts, scope, [{
            'id': 'formula', 'operator': 'FORMULA_CHECK',
            'inputRefs': ['root'], 'checkScope': check_scope,
        }], RuleRegistry())
        return {'facts': facts, 'derivedValues': derived, 'scope': scope}

    def document(self, field, **extra):
        return {
            'title': '差额核对',
            'html': '<html><head></head><body><p>差额 {{difference}} 元</p></body></html>',
            'bindings': [{'key': 'difference', 'valueRef': 'derived_formula',
                          'field': field, 'format': {'grouping': True, 'decimalPlaces': 2}, **extra}],
        }

    def test_local_success_does_not_make_recursive_difference_available(self):
        artifact = self.artifact()
        value = artifact['derivedValues'][0]
        self.assertEqual(value['calculationValidation'], 'PASS')
        self.assertEqual(value['formulaTrace']['status'], 'BLOCKED')
        before = deepcopy(artifact)
        for field, suggested in [('difference', 'localDifference'),
                                 ('absoluteDifference', 'localAbsoluteDifference')]:
            with self.subTest(field=field), self.assertRaises(AnalysisError) as caught:
                compile_document(artifact, self.document(field))
            details = caught.exception.details
            self.assertEqual(details['path'], '/document/bindings/0')
            self.assertEqual(details['valueRef'], 'derived_formula')
            self.assertEqual(details['valueField'], field)
            self.assertEqual(details['suggestedField'], suggested)
            self.assertIn(suggested, details['availableFields'])
            self.assertNotIn(field, details['availableFields'])
            self.assertIn('无需重新取数', details['hint'])
        self.assertEqual(artifact, before)

    def test_local_signed_and_absolute_differences_are_visible(self):
        artifact = self.artifact()
        for field, expected in [('localDifference', '-1,322,980.43'),
                                ('localAbsoluteDifference', '1,322,980.43')]:
            with self.subTest(field=field):
                compiled = compile_document(artifact, self.document(field))
                self.assertIn('<p>差额 ' + expected + ' 元</p>', compiled['html'])

    def test_default_value_respects_operation_scope_but_node_value_is_recursive(self):
        artifact = self.artifact()
        value = artifact['derivedValues'][0]
        self.assertEqual(selected_value(value), '1314680.43')
        self.assertTrue(executable_field(value))
        self.assertFalse(executable_field(value, 'value', 'root'))
        self.assertIn('1,314,680.43', compile_document(artifact, self.document('value'))['html'])
        with self.assertRaises(AnalysisError):
            compile_document(artifact, self.document('value', nodeRef='root'))

    def test_blocked_recursive_operation_keeps_usable_local_and_source_fields(self):
        artifact = self.artifact('RECURSIVE')
        self.assertEqual(artifact['derivedValues'][0]['calculationValidation'], 'BLOCKED')
        for field, expected in [('localValue', '1,314,680.43'),
                                ('localAbsoluteDifference', '1,322,980.43'),
                                ('sourceValue', '-8,300.00')]:
            self.assertIn(expected, compile_document(artifact, self.document(field))['html'])
        with self.assertRaises(AnalysisError):
            compile_document(artifact, self.document('value'))

    def test_completed_recursive_difference_keeps_its_original_meaning(self):
        artifact = self.artifact(missing=False)
        value = artifact['derivedValues'][0]
        self.assertNotEqual(selected_value(value, 'localAbsoluteDifference'),
                            selected_value(value, 'absoluteDifference'))
        compiled = compile_document(artifact, self.document('absoluteDifference'))
        self.assertIn('<p>差额 1,537,393.00 元</p>', compiled['html'])

    def test_zero_difference_remains_available(self):
        artifact = self.artifact()
        facts = artifact['facts']
        facts[0]['value'] = '1314680.43'
        artifact['derivedValues'] = calculate(facts, artifact['scope'], [{
            'id': 'formula', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root'], 'checkScope': 'LOCAL',
        }], RuleRegistry())
        self.assertIn('<p>差额 0.00 元</p>',
                      compile_document(artifact, self.document('localAbsoluteDifference'))['html'])

    def test_source_null_and_missing_unit_are_preserved(self):
        artifact = self.artifact()
        document = self.document('value', valueRef='cost')
        compiled = compile_document(artifact, document)
        payload = json.loads(re.search(r'<script id="analysis-host-data"[^>]*>(.*?)</script>',
                                       compiled['html'], re.S).group(1))
        self.assertIsNone(payload['values']['difference']['value'])
        self.assertEqual(payload['values']['difference']['display'], '')
        document['bindings'][0] = {'key': 'difference', 'valueRef': 'root', 'field': 'unit'}
        self.assertIn('<p>差额  元</p>', compile_document(artifact, document)['html'])

    def test_invalid_node_lists_actual_choices(self):
        with self.assertRaises(AnalysisError) as caught:
            compile_document(self.artifact(), self.document('localDifference', nodeRef='missing'))
        self.assertIn('root', caught.exception.details['allowedNodeRefs'])
        self.assertEqual(caught.exception.details['nodeRef'], 'missing')

    def test_claims_and_html_return_same_actionable_field_details(self):
        value = self.artifact()['derivedValues'][0]
        with self.assertRaises(AnalysisError) as caught:
            render_parts({'derived_formula': value}, [
                {'text': '相差 '}, {'valueRef': 'derived_formula', 'field': 'absoluteDifference'},
            ])
        self.assertEqual(caught.exception.details['relativePath'], 'parts/1')
        self.assertEqual(caught.exception.details['suggestedField'], 'localAbsoluteDifference')

    def test_completed_operation_without_scalar_cannot_render_as_a_number(self):
        artifact = self.artifact()
        artifact['facts'][0]['formula'] = None
        artifact['derivedValues'] = calculate(artifact['facts'], artifact['scope'], [{
            'id': 'formula', 'operator': 'FORMULA_CHECK', 'inputRefs': ['root'],
        }], RuleRegistry())
        self.assertEqual(artifact['derivedValues'][0]['calculationValidation'], 'PASS')
        with self.assertRaises(AnalysisError):
            compile_document(artifact, self.document('value'))
        # 没有可复算公式仍可展示已经取得的来源数。
        self.assertIn('-8,300.00', compile_document(artifact, self.document('sourceValue'))['html'])

    def test_document_schema_rejects_invented_formula_fields(self):
        valid = self.document('localAbsoluteDifference')['bindings'][0]
        validator = Draft202012Validator(DOCUMENT_BINDING)
        self.assertEqual(list(validator.iter_errors(valid)), [])
        invalid = {**valid, 'field': 'recursiveAbsoluteDifference'}
        self.assertTrue(list(validator.iter_errors(invalid)))
