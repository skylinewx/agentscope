from copy import deepcopy
import unittest

from analysis_extension.readable_material import record_table, restore_table, readable_analysis_material, restore_material
from analysis_extension.input_contract import field_catalog
from analysis_extension.python_analysis import input_record


class ReadableMaterialTests(unittest.TestCase):
    def test_execution_column_descriptions_share_without_merging_snapshots(self):
        from analysis_extension.readable_material import business_record_tables
        records = [{'record': {'ref': f'input-{period}-{metric}',
                    'dimensions': {'period': str(period)}, 'label': 'Source metric ' + str(metric),
                    'columnName': 'metric' + str(metric), 'scaleBasis': None if metric == 0 else 'source_unit_label',
                    'metricKey': f'unique-observation-{period}-{metric}',
                    'value': None if period == 0 else str(period), 'customField': {'flag': False}}}
                   for period in range(40) for metric in range(3)]
        before = deepcopy(records)
        table = business_record_tables(records, input_context=True)
        restored = restore_material({'facts': [], 'pythonRuns': [], 'sample': table})['sample']
        self.assertEqual(restored, before)
        self.assertEqual(records, before)
        self.assertEqual(table['columnDefinitions']['rowCount'], 3)
        self.assertEqual(table['observations']['rowCount'], 120)

    def test_tables_preserve_types_missing_nested_paths_and_special_characters(self):
        records = [{'value': None, 'text': 'a|b\\c\n<br>&amp;', 'new/dim~': {'x': '0.000100'}, 'bool': False},
                   {'value': '0', 'text': 'MISSING', 'new/dim~': {}, 'bool': True}, {'value': 0, 'text': ' \t keep trailing \u3000', 'new/dim~': None}]
        self.assertEqual(restore_table(record_table(records)), records)

    def test_records_and_actual_execution_snapshots_round_trip_without_mutation(self):
        facts = [{'factId': f'f{i}', 'expression': 'TABLE[M]', 'dimensions': {'MD_ORG': str(i // 2), 'X': '001'},
                  'sourceMeasureCode': str(i % 2), 'value': None if i == 1 else str(i), 'valueState': 'NULL' if i == 1 else 'PRESENT',
                  'inputRole': 'PRIMARY', 'headers': ['名称'], 'unit': '元'} for i in range(20)]
        inputs = [input_record(fact['factId'], fact) for fact in facts]
        changed = deepcopy(inputs); changed[1]['value'] = '0'; changed[1]['inputRole'] = 'REFERENCE'
        material = {'facts': facts, 'sourceScopes': [{'columns': [{'name': 'M', 'label': '金额', 'unit': '元'}]}] * 3,
                    'pythonRuns': [{'runRef': name, 'inputRecords': rows, 'code': 'free code'}
                                   for name, rows in [('r1', inputs), ('r2', inputs), ('r3', changed)]],
                    'derived': [{'derivedId': 'd', 'inputRefs': [row['ref'] for row in inputs]}]}
        before = deepcopy(material)
        projected = readable_analysis_material(material)
        self.assertEqual(restore_material(projected), before)
        self.assertEqual(material, before)
        self.assertEqual(len(projected['executionInputSets']), 2)

    def test_dynamic_business_fields_and_structures_survive_runtime_catalog(self):
        value = {'value': None, 'dimensions': {'NEW_DIM': 'A'}, 'customBusinessField': {'new': 1},
                 'columns': [{'name': 'custom'}], 'rows': [['a']], 'sourceOrigins': ['audit-only']}
        record = input_record('ref', value)
        self.assertEqual(record['customBusinessField'], {'new': 1})
        self.assertNotIn('sourceOrigins', record)
        catalog = field_catalog([record, {**record, 'value': '0'}], examples=1)
        self.assertEqual(next(field for field in catalog['fields'] if field['name'] == 'value')['types'], ['null', 'string'])
        self.assertFalse(catalog['examplesAreCompleteData'])
        self.assertEqual(catalog['recordCount'], 2)

    def test_examples_are_bounded_without_truncating_runtime_inputs(self):
        record = input_record('table', {'rows': [[i, 'value'] for i in range(10000)], 'columns': [{'name': 'x'}]})
        catalog = field_catalog([record])
        self.assertFalse(catalog['examplesAreCompleteData'])
        self.assertTrue(catalog['exampleOmissions'])
        self.assertEqual(len(record['rows']), 10000)
        self.assertEqual(len(catalog['examples'][0]['rows']), 3)

    def test_checked_business_selection_preserves_scope_and_order(self):
        facts = [{'factId': f'fact_{i:04}', 'objectId': f'member_{i // 2}', 'period': f'P{i % 4}',
                  'dimensions': {'channel': str(i % 3)}, 'value': str(i), 'headers': ['金额'], 'unit': '元'} for i in range(60)]
        selected = [fact['factId'] for fact in facts if fact['period'] == 'P1']
        scope = {'members': sorted({fact['objectId'] for fact in facts}), 'observedMembers': sorted({fact['objectId'] for fact in facts})}
        source = {'facts': facts, 'scope': scope, 'derived': [{'inputRefs': selected}, {'inputRefs': selected[::-1]}]}
        self.assertEqual(restore_material(readable_analysis_material(source)), source)
