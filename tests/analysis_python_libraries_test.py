"""真实运行开放导入与金额计算，并核对第三方库的隔离和缺数语义。"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from analysis_extension.models import AnalysisError
from analysis_extension.python_analysis import execute_python
from analysis_extension.python_environment import execution_plan, fingerprint
from analysis_extension.python_worker import validate_code


class StandardImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_decimal_alias_collections_statistics_and_plain_output(self):
        output = await execute_python('''
from decimal import Decimal as D
from collections import defaultdict
from statistics import mean
groups = defaultdict(list)
for item in inputs:
    groups[item['group']].append(D(item['value']))
result = [{'sum': sum(groups['a']), 'average': mean(groups['a']),
           'tiny': D('1E-8'), 'zero': D('0E+5'), 'division': 1 / 8}]
''', [{'group': 'a', 'value': '0.1'}, {'group': 'a', 'value': '0.2'}], 10)
        self.assertEqual(output, [{'sum': '0.3', 'average': '0.15', 'tiny': '0.00000001', 'zero': '0', 'division': '0.125'}])
        self.assertEqual(output.runtime_metadata['runtime'], 'RESTRICTED_PYTHON')
        self.assertIn('python', output.runtime_metadata['interpreter'])

    async def test_module_import_alias_and_memory_json(self):
        output = await execute_python('''
import decimal as dec
import json as js
from itertools import chain
items = js.loads('["0.1", "0.2"]')
result = [{'amount': sum(dec.Decimal(item) for item in chain(items))}]
''', [], 10)
        self.assertEqual(output[0]['amount'], '0.3')

    def test_unsupported_imports_fail_before_execution(self):
        for code in ('import os', 'import subprocess', 'from pathlib import Path',
                     'from decimal import *', 'from decimal import getcontext',
                     'import pandas as inputs', 'import numpy as D', 'from . import x',
                     'import pandas as pd\nresult=pd.read_pickle("/etc/passwd")'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_code(code)

    async def test_missing_container_never_runs_libraries_on_host(self):
        with patch('analysis_extension.python_environment.prepare', AsyncMock(side_effect=AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', 'offline'))):
            with self.assertRaises(AnalysisError) as caught:
                await execute_python('import pandas as pd\nresult=[{}]', [], 10)
        self.assertEqual(caught.exception.code, 'PYTHON_ENVIRONMENT_UNAVAILABLE')

    async def test_container_plan_exposes_only_explicit_grid_files(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'grid.jsonl'
            file.write_text('{}\n', 'utf-8')
            inputs = [{'kind': 'GRID_FILE', '_path': str(file), 'ref': 'grid'}]
            with patch('analysis_extension.python_environment.prepare', AsyncMock(return_value={
                    'docker': 'docker', 'environmentId': 'lock', 'imageId': 'sha256:fixed'})):
                plan = await execution_plan(validate_code('import numpy as np'), inputs)
            self.assertEqual(inputs[0]['_path'], str(file))
            self.assertEqual(plan['inputs'][0]['_path'], '/inputs/grid-0.jsonl')
            self.assertIn('--read-only', plan['command'])
            self.assertEqual(plan['command'][plan['command'].index('--network') + 1], 'none')
            mounts = [plan['command'][index + 1] for index, arg in enumerate(plan['command']) if arg == '--mount']
            self.assertEqual(len(mounts), 2)
            self.assertTrue(all(mount.endswith(',readonly') for mount in mounts))
            self.assertNotEqual(fingerprint(False), fingerprint(True))


@unittest.skipUnless(os.environ.get('RUN_ANALYSIS_LIBRARY_TESTS') == '1', '真实 Docker 分析库回归需显式启用')
class IsolatedLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pandas_decimal_grouping_preserves_empty_group_and_null(self):
        output = await execute_python('''
import pandas as pd
from decimal import Decimal as D
df = pd.DataFrame(inputs, dtype=object)
df['value'] = df['value'].map(lambda value: D(value) if value is not None else None)
totals = df.groupby('group', sort=False)['value'].sum().reset_index()
result = [{'rows': totals, 'empty': pd.Series([None, None], dtype=object).sum()}]
''', [{'group': 'a', 'value': '0.1'}, {'group': 'a', 'value': '0.2'},
      {'group': 'empty', 'value': None}, {'group': None, 'value': '9007199254740993.11'}], 30)
        self.assertEqual(output[0]['rows'], [['a', '0.3'], ['empty', None], [None, '9007199254740993.11']])
        self.assertIsNone(output[0]['empty'])
        self.assertEqual(output.runtime_metadata['runtime'], 'ISOLATED_ANALYSIS_LIBRARIES')
        self.assertEqual(output.runtime_metadata['interpreter']['system'], 'Linux')

    async def test_numpy_decimal_arrays_and_normal_import_from(self):
        output = await execute_python('''
from numpy import array
from decimal import Decimal
import json as js
check = js.loads('["0.1"]')
amounts = array([Decimal('0.1'), Decimal('0.2')], dtype=object)
result = [{'rows': amounts / 2, 'total': amounts.sum()}]
''', [], 30)
        self.assertEqual(output, [{'rows': ['0.05', '0.1'], 'total': '0.3'}])

    async def test_scientific_libraries_ready_but_float_results_are_not_money(self):
        output = await execute_python('''
import scipy.stats as stats
import statsmodels.api as sm
result = [{'ready': True}]
''', [], 30)
        self.assertEqual(output, [{'ready': True}])
        with self.assertRaises(AnalysisError) as caught:
            await execute_python('import pandas as pd\nresult=[{"amount": pd.Series([1, 2]).mean()}]', [], 30)
        self.assertIn('二进制浮点', caught.exception.details['message'])

    async def test_library_worker_reads_only_authorized_grid_and_keeps_read_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'grid.jsonl'
            rows = [{'key': f'{index}:0', 'row': index, 'column': 0, 'rawValueJson': json.dumps(amount),
                     'dimensions': {}, 'measureCode': 'amount'} for index, amount in enumerate(('0.1', '0.2'))]
            file.write_text(''.join(json.dumps(row) + '\n' for row in rows), 'utf-8')
            output = await execute_python('''
import pandas as pd
from decimal import Decimal as D
ref = inputs[0]['ref']
amounts = pd.Series([D(row['value']) for row in grid_rows(ref)], dtype=object)
result = [{'value': amounts.sum()}]
''', [{'kind': 'GRID_FILE', 'ref': 'grid', '_path': str(file), 'sha256': 'test-hash', 'cellCount': 2}], 30)
        self.assertEqual(output[0]['value'], '0.3')
        self.assertTrue(output.file_reads[0]['exhausted'])
        self.assertEqual(output.file_reads[0]['selectedKeys'], ['0:0', '1:0'])

    async def test_container_timeout_removes_running_execution(self):
        with self.assertRaises(AnalysisError) as caught:
            await execute_python('import numpy as np\nwhile True:\n    pass', [], 2)
        self.assertEqual(caught.exception.code, 'PYTHON_TIMEOUT')
