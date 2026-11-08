from unittest import TestCase

from analysis_business_query_test import business_fixture, update
from analysis_extension.models import AnalysisError
from analysis_extension.prepared_inputs import _confirmed_grain


class ConfirmedPreparedGrainTest(TestCase):
    def fixture(self, dimension='MD_ORG', label='组织机构'):
        context = {'target': {'formBindingKey': 'BUDTASK/T|BUDVIEW/V'},
                   'effectiveDimensions': {dimension: 'object-1', 'SCENE': 'BUDGET'}}
        form = {'preparation': {'targetKeys': ['3:5']},
                'cells': [{'key': '3:5', 'modelCode': 'MODEL_N', 'dimensions': {'ACCOUNT': 'profit'}}]}
        data = {'dimensionCatalog': [{'dimCode': dimension, 'modelCode': 'MODEL', 'label': label, 'names': [label]}],
                'scopeResolution': [{'dimCode': dimension, 'binding': 'BUDTASK/T|BUDVIEW/V', 'status': 'RESOLVED',
                                     'condition': {'dimCode': dimension, 'hierarchy': {'rootCode': 'root', 'level': 2}}},
                                    {'dimCode': 'SCENE', 'binding': 'BUDTASK/T|BUDVIEW/V', 'status': 'RESOLVED',
                                     'condition': {'dimCode': 'SCENE', 'values': ['BUDGET']}}]}
        capture, receipt = business_fixture()
        receipt['data'].update(data)
        update(capture, receipt)
        return capture, form, context, receipt

    def test_actual_catalog_resolves_names_for_any_dimension(self):
        for code, label in [('MD_ORG', '组织机构'), ('MD_DEPT', '部门'), ('MD_PROJECT', '项目')]:
            capture, form, context, _ = self.fixture(code, label)
            self.assertEqual(_confirmed_grain(capture, form, context, [label]), [code])
            self.assertEqual(_confirmed_grain(capture, form, context, [code]), [code])

    def test_confirmed_range_is_grain_even_when_only_one_filtered_member_remains(self):
        capture, form, context, _ = self.fixture()
        self.assertEqual(_confirmed_grain(capture, form, context), ['MD_ORG'])

    def test_fixed_values_and_other_form_ranges_do_not_become_subject_grain(self):
        capture, form, context, receipt = self.fixture()
        receipt['data']['scopeResolution'][0]['binding'] = 'BUDTASK/T|BUDVIEW/OTHER'
        update(capture, receipt)
        self.assertIsNone(_confirmed_grain(capture, form, context))

    def test_ambiguous_or_unavailable_name_is_not_silently_mapped(self):
        capture, form, context, receipt = self.fixture()
        context['effectiveDimensions']['OTHER_ORG'] = 'other'
        receipt['data']['dimensionCatalog'].append({'dimCode': 'OTHER_ORG', 'modelCode': 'MODEL', 'label': '组织机构'})
        update(capture, receipt)
        for names in (['组织机构'], ['不存在'], ['MD_ORG', 'MD_ORG']):
            with self.assertRaises(AnalysisError):
                _confirmed_grain(capture, form, context, names)
        self.assertEqual(_confirmed_grain(capture, form, context, ['MD_ORG']), ['MD_ORG'])

    def test_same_label_in_unrelated_model_does_not_make_current_name_ambiguous(self):
        capture, form, context, receipt = self.fixture()
        context['effectiveDimensions']['OTHER_ORG'] = 'other'
        receipt['data']['dimensionCatalog'].append({'dimCode': 'OTHER_ORG', 'modelCode': 'OTHER_MODEL', 'label': '组织机构'})
        update(capture, receipt)
        self.assertEqual(_confirmed_grain(capture, form, context, ['组织机构']), ['MD_ORG'])

    def test_model_prefix_is_not_a_model_binding(self):
        capture, form, context, receipt = self.fixture()
        form['cells'][0]['modelCode'] = 'MODEL_DETAIL_N'
        receipt['data']['dimensionCatalog'][0]['modelCode'] = 'MODEL_DETAIL'
        context['effectiveDimensions']['OTHER_ORG'] = 'other'
        receipt['data']['dimensionCatalog'].append({'dimCode': 'OTHER_ORG', 'modelCode': 'MODEL', 'label': '组织机构'})
        update(capture, receipt)
        self.assertEqual(_confirmed_grain(capture, form, context, ['组织机构']), ['MD_ORG'])
