from copy import deepcopy
from unittest import TestCase

from analysis_extension.business_records import record_identity, record_catalog


class BusinessRecordTests(TestCase):
    def fact(self, **changes):
        return {'factId': 'f1', 'expression': 'TABLE_A[AMOUNT,0N]',
                'dimensions': {'ORG': '001', 'YEAR': '2026', 'SCENE': 'BUDGET'},
                'value': '10', 'sourceMeasureCode': 'AMOUNT', **changes}

    def test_only_table_and_complete_dimensions_identify_record(self):
        first = self.fact()
        second = self.fact(factId='f2', value='20', headers=['其他名称'], inputRole='REFERENCE',
                           expression='TABLE_A[QUANTITY,0N]', taskName='另一表样任务', sourceMeasureCode='QUANTITY')
        self.assertEqual(record_identity(first), record_identity(second))
        second['dimensions'] = dict(reversed(list(second['dimensions'].items())))
        self.assertEqual(record_identity(first), record_identity(second))

    def test_different_table_or_dimension_is_not_same_record(self):
        original = record_identity(self.fact())
        self.assertNotEqual(original, record_identity(self.fact(expression='TABLE_B[AMOUNT,0N]')))
        for key, value in [('ORG', '002'), ('YEAR', '2025'), ('SCENE', 'ACTUAL'), ('ORG', 1)]:
            fact = self.fact(); fact['dimensions'][key] = value
            self.assertNotEqual(original, record_identity(fact))

    def test_effective_dimensions_are_included_and_row_overrides_win(self):
        fact = self.fact(effectiveDimensions={'CURRENCY': 'CNY', 'ORG': 'root'})
        self.assertEqual(record_identity(fact)['dimensions']['ORG'], '001')
        other = deepcopy(fact); other['effectiveDimensions']['CURRENCY'] = 'USD'
        self.assertNotEqual(record_identity(fact), record_identity(other))

    def test_missing_address_and_aggregate_are_not_guessed(self):
        for change in ({'expression': None}, {'dimensions': {}}, {'aggregation': {'type': 'SUM'}}, {'calcType': 'SUM'}):
            self.assertIsNone(record_identity(self.fact(**change)))

    def test_shared_record_keeps_fields_snapshots_and_roles(self):
        facts = [self.fact(), self.fact(factId='f2', sourceMeasureCode='QUANTITY', value=None),
                 self.fact(factId='f3', value='11', inputRole='REFERENCE')]
        before = deepcopy(facts); catalog = record_catalog(facts)
        self.assertEqual(catalog['recordCount'], 1)
        self.assertEqual(catalog['factCount'], 3)
        self.assertEqual([o['value'] for o in catalog['records'][0]['observations']], ['10', None, '11'])
        self.assertEqual(facts, before)

    def test_source_namespaces_do_not_merge(self):
        self.assertEqual(record_catalog([self.fact(sourceContextId='a'), self.fact(sourceContextId='b')])['recordCount'], 2)
