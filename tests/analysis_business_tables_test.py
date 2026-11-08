import copy
import unittest

from analysis_extension.business_tables import build_tables, business_view, resolve_cell, actual_periods, model_text
from analysis_extension.models import AnalysisError


def fact(key, dimensions, value='1.20', measure='ZB_JE2', title='净利润_预算数', **extra):
    return {'factId': key, 'dimensions': dimensions, 'dimensionNames': {'MD_SCENE': {'BUDGET': '预算数', 'ACTUAL': '实际数'}.get(dimensions.get('MD_SCENE')), 'MD_BUDACCOUNT': dimensions.get('MD_BUDACCOUNT')},
            'value': value, 'valueState': 'PRESENT', 'valueType': 'DECIMAL_STRING', 'unit': '元',
            'measureCode': measure, 'title': title, **extra}


def receipt(rows, **extra):
    return {'status': 'SUCCEEDED', 'data': {'resultType': 'BUDGET_QUERY_RESULT', 'purpose': 'QUERY',
            'complete': True, 'resultSets': [{'resultSetId': 's1', 'interpretationId': 'i1',
                'context': {'taskName': '任务甲', 'formName': '利润表'}, 'rows': rows}], **extra}}


class BusinessTablesTest(unittest.TestCase):
    def test_same_scenario_code_does_not_split_when_some_names_are_missing(self):
        rows = [fact('a', {'MD_BUDACCOUNT': 'a', 'MD_SCENE': 'BUDGET'}, dimensionNames={}),
                fact('b', {'MD_BUDACCOUNT': 'b', 'MD_SCENE': 'BUDGET'})]
        for ordered in (rows, list(reversed(rows))):
            table = build_tables(receipt(ordered))[0]
            self.assertEqual(1, len(table.columns))
            self.assertEqual('预算数', table.columns[0]['label'])
            self.assertEqual({'a', 'b'}, set(table.locations))
            self.assertNotIn('未返回', table.markdown())

    def test_scenario_columns_and_multiple_accounts_are_lossless(self):
        rows = [fact(str(i), {'MD_BUDACCOUNT': account, 'MD_SCENE': scene, 'DATATIME': '2026N0001'}, str(i))
                for i, (account, scene) in enumerate([('收入', 'BUDGET'), ('收入', 'ACTUAL'), ('利润', 'BUDGET'), ('利润', 'ACTUAL')])]
        original = copy.deepcopy(rows)
        result = business_view(receipt(rows), 'cap1')
        self.assertTrue(result['allRowsIncluded'])
        self.assertNotIn('ZB_JE2', result['markdown'])
        self.assertIn('预算数（元）', result['markdown'])
        self.assertEqual(2, result['tables'][0]['rowCount'])
        self.assertEqual('0', resolve_cell(receipt(rows), 'T1', 'R1', 'C1')['value'])
        self.assertEqual(original, rows)

    def test_single_scenario_stays_a_column_and_null_is_not_zero(self):
        rows = [fact('1', {'MD_SCENE': 'BUDGET'}, '0'), fact('2', {'MD_SCENE': 'BUDGET'}, None, valueState='NULL')]
        view = business_view(receipt(rows), 'cap1')
        self.assertIn('| R1 | 0 |', view['markdown'])
        self.assertIn('| R2 | 空值 |', view['markdown'])
        self.assertEqual(2, view['tables'][0]['rowCount'])
        with self.assertRaises(AnalysisError):
            resolve_cell(receipt(rows), 'T1', 'R2', 'C1')

    def test_tasks_do_not_merge_and_partial_is_not_display_pagination(self):
        row = fact('1', {'MD_SCENE': 'BUDGET'})
        source = receipt([row]); second = copy.deepcopy(source['data']['resultSets'][0]); second['context']['taskName'] = '任务乙'
        source['data']['resultSets'].append(second)
        source.update(status='PARTIAL'); source['data']['complete'] = False
        view = business_view(source, 'cap1')
        self.assertEqual(2, view['tableCount'])
        self.assertTrue(view['allRowsIncluded'])
        self.assertFalse(view['complete'])

    def test_period_coverage_has_no_invented_quarter(self):
        rows = [fact(str(n), {'DATATIME': f'2026J000{n}', 'MD_SCENE': 'BUDGET'}) for n in (1, 3)]
        self.assertEqual('2026年第1季度、2026年第3季度', actual_periods(rows))

    def test_ordinary_measures_pivot_without_arithmetic(self):
        rows = [fact(f'{n}-{m}', {'区域': str(n)}, str(n), measure=m, title=m, unit=None)
                for n in range(4) for m in ('数量', '收入')]
        table = build_tables(receipt(rows))[0]
        self.assertEqual(4, len(table.rows)); self.assertEqual(2, len(table.columns))
        self.assertEqual(set(f['factId'] for f in rows), set(table.locations))

    def test_paging_is_stable_and_wrong_capture_is_rejected(self):
        source = receipt([fact(str(n), {'科目': str(n), 'MD_SCENE': 'BUDGET'}) for n in range(15)])
        first = business_view(source, 'cap1', limit=6)
        nxt = first['nextRead']; nxt.pop('captureRef')
        second = business_view(source, 'cap1', **nxt)
        self.assertIn('| R7 |', second['markdown']); self.assertNotIn('| R1 |', second['markdown'])
        self.assertFalse(first['allRowsIncluded']); self.assertFalse(second['allRowsIncluded'])
        with self.assertRaises(AnalysisError): business_view(source, 'cap2', **nxt)
        filtered = business_view(source, 'cap1', tableRef='T1', filters=[{'column': '科目', 'operator': 'EQ', 'value': '10'}])
        self.assertIn('| R11 |', filtered['markdown'])

    def test_escape_and_markdown_is_not_serialized_body(self):
        row = fact('1', {'科目': 'a|b\nc', 'MD_SCENE': 'BUDGET'})
        row2 = fact('2', {'科目': 'd', 'MD_SCENE': 'BUDGET'})
        view = business_view(receipt([row, row2]), 'cap1')
        body = model_text(view)
        self.assertIn('a\\|b<br>c', body)
        self.assertNotIn('"markdown":', body)
        self.assertIn('\n| 行号', body)

    def test_same_name_member_filter_is_ambiguous(self):
        rows = [fact(str(n), {'科目': str(n)}, dimensionNames={'科目': '同名'}) for n in (1, 2)]
        with self.assertRaises(AnalysisError):
            business_view(receipt(rows), 'cap1', tableRef='T1', filters=[{'column': '科目', 'operator': 'EQ', 'value': '同名'}])

    def test_wide_row_column_pages_never_skip_other_rows_columns(self):
        rows = [fact(f'{r}-{c}', {'区域': str(r)}, str(c), measure=f'M{c}', title='指标'+str(c)) for r in (1, 2) for c in range(30)]
        source = receipt(rows); request = {}; seen = []
        for _ in range(50):
            view = business_view(source, 'cap1', max_chars=3000, **request)
            for table in view['tables']:
                import re
                displayed = re.findall(r'^\| (R\d+) \|', view['markdown'], re.M)
                seen.extend((row, col['id']) for row in displayed for col in table['columns'])
            if view['readComplete']: break
            request = {k: v for k, v in view['nextRead'].items() if k != 'captureRef'}
        self.assertEqual(60, len(seen)); self.assertEqual(60, len(set(seen)))


if __name__ == '__main__':
    unittest.main()
