"""Real container boundary tests, explicitly separated from host protocol tests."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import analysis_fixtures
from analysis_extension.execution_runner import execute
from analysis_extension.models import AnalysisError


@unittest.skipUnless(os.environ.get('RUN_ANALYSIS_RUNTIME_TESTS') == '1', 'requires real prepared Docker runtime')
class IsolatedExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_named_inputs_above_old_inline_limit_reach_real_container(self):
        inputs = [{'ref':'f'+str(i), 'value':'1.005', 'headers':['x'*900]} for i in range(5001)]
        result = await execute("result=[{'count':len(inputs), 'total':sum(D(i['value']) for i in inputs).quantize(D('.01'))}]", inputs, 30)
        self.assertEqual(result, [{'count':5001, 'total':'5026.01'}])
        self.assertTrue(result.input_delivery_manifest['inlineComplete'])
        self.assertEqual(len(result.input_delivery_manifest['inlineRefs']), 5001)
        self.assertEqual(result.runtime_metadata['cleanupStatus'], 'REMOVED')

    async def test_cancellation_cleans_container_including_child_process(self):
        from unittest.mock import patch
        from analysis_extension.execution_environment import DockerBackend
        ready = asyncio.Event()
        class Observed(DockerBackend):
            async def start(self, plan):
                process = await super().start(plan)
                ready.set()
                return process
        with patch('analysis_extension.execution_runner.backend_factory', Observed):
            task = asyncio.create_task(execute("import subprocess,time\nsubprocess.Popen(['sleep','60'])\ntime.sleep(60)\nresult=[{}]", [], 30))
            await asyncio.wait_for(ready.wait(), 20)
            await asyncio.sleep(.4)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertEqual(caught.exception.execution_evidence['runtime']['cleanupStatus'], 'REMOVED')

    async def test_restart_sweeper_reclaims_only_expired_owned_container(self):
        from analysis_extension.execution_environment import DockerBackend
        from analysis_extension.execution_recovery import reap_expired_containers
        backend = DockerBackend()
        plan = await backend.prepare(session='recovery-test', run_ref='python_recovery_fixture', timeout_seconds=10)
        process = await backend.start(plan)
        try:
            from contextlib import closing
            with closing(plan['gate'].connect()) as db:
                db.execute('UPDATE leases SET deadline=0 WHERE id=?', (plan['runRef'],))
                db.commit()
            self.assertGreaterEqual(await reap_expired_containers(), 1)
            await asyncio.wait_for(process.wait(), 5)
            with closing(plan['gate'].connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM leases WHERE id=?', (plan['runRef'],)).fetchone()[0], 0)
        finally:
            await backend.cleanup(plan)

    async def test_ordinary_methods_and_unrelated_import_share_policy(self):
        code = '''
from decimal import Decimal
class Value:
    def amount(self):
        try:
            assert isinstance(D('1.005'), Decimal)
            return D('-1.005').copy_abs().normalize().scaleb(0).quantize(D('.01'))
        except Exception:
            raise
result=[{'value':Value().amount(), 'ratio':1/8}]
'''
        ordinary = await execute(code, [], 20)
        scientific = await execute('import pandas as pd\n' + code, [], 20)
        self.assertEqual(ordinary, scientific)
        self.assertEqual(ordinary[0], {'value': '1.01', 'ratio': '0.125'})
        self.assertEqual(ordinary.runtime_metadata['runtime'], 'ISOLATED_PYTHON')
        self.assertEqual(ordinary.runtime_metadata['cleanupStatus'], 'REMOVED')

    async def test_grid_delivery_is_host_recorded_and_file_is_not_mounted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host-grid.jsonl'
            rows = [{'key':str(i),'row':i,'column':0,'measureCode':'amount','rawValueJson':json.dumps(str(i))} for i in range(1200)]
            raw = b''.join(json.dumps(row).encode() + b'\n' for row in rows)
            path.write_bytes(raw)
            grid = {'ref':'grid-A','kind':'GRID_FILE','_path':str(path),'byteSize':len(raw),
                    'sha256':hashlib.sha256(raw).hexdigest(),'cellCount':len(rows)}
            result = await execute('''
from pathlib import Path
assert '_path' not in inputs[0]
assert not Path('/inputs/grid-0.jsonl').exists()
total = sum(D(cell['value']) for cell in grid_rows(inputs[0]['ref']))
result=[{'value':total}]
''', [grid], 20)
            self.assertEqual(result[0]['value'], str(sum(range(1200))))
            self.assertEqual(result.input_delivery_manifest['reads'][0]['sentCount'], 1200)
            self.assertTrue(result.input_delivery_manifest['reads'][0]['sendComplete'])

    async def test_network_readonly_root_and_no_host_secret(self):
        result = await execute('''
import os, socket
from pathlib import Path
readonly = False
try:
    Path('/forbidden-write').write_text('x')
except OSError:
    readonly = True
network = False
try:
    socket.create_connection(('1.1.1.1',443), timeout=1).close()
    network = True
except OSError:
    pass
Path('/tmp/allowed').write_text('ok')
result=[{'readonly':readonly,'network':network,'uid':os.getuid(),
         'socket':Path('/var/run/docker.sock').exists(),'private_workspace':Path('/workspace').exists()}]
''', [], 10)
        self.assertEqual(result, [{'readonly':True,'network':False,'uid':65534,'socket':False,'private_workspace':False}])

    async def test_timeout_terminates_task_and_releases_resources(self):
        with self.assertRaises(AnalysisError) as caught:
            await execute('while True: pass', [], 1)
        self.assertEqual(caught.exception.code, 'PYTHON_TIMEOUT')
        self.assertEqual(caught.exception.execution_evidence['runtime']['cleanupStatus'], 'REMOVED')
        result = await execute('result=[{"value":D("2.50")}]', [], 10)
        self.assertEqual(result[0]['value'], '2.50')

    async def test_caught_unauthorized_read_still_cannot_create_trusted_success(self):
        with self.assertRaises(AnalysisError):
            await execute('''
try:
    list(grid_rows('some-other-session'))
except Exception:
    pass
result=[{'value':1}]
''', [], 10)

    async def test_large_allocation_is_limited_inside_container(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'ANALYSIS_PYTHON_MEMORY_BYTES': str(256 * 1024**2)}):
            with self.assertRaises(AnalysisError) as caught:
                await execute('result=[0] * 150000000', [], 15)
        self.assertIn(caught.exception.code, ('PYTHON_EXECUTION_FAILED', 'PYTHON_TIMEOUT'))
        self.assertEqual(caught.exception.execution_evidence['runtime']['memoryLimitBytes'], 256 * 1024**2)
        self.assertEqual(caught.exception.execution_evidence['runtime']['cleanupStatus'], 'REMOVED')

    async def test_bounded_logs_do_not_corrupt_result_protocol(self):
        output = await execute('print("x"*1200000)\nresult=[{"value":D("1.01")}]', [], 10)
        self.assertEqual(output[0]['value'], '1.01')
        self.assertTrue(output.runtime_metadata['logTruncated'])

    async def test_forged_source_record_in_result_is_rejected(self):
        with self.assertRaises(AnalysisError):
            await execute('''
import os
from execution_protocol import write_message
write_message(grid_rows.__self__.target, {'type':'RESULT','result':[{'value':1}],
              'inputDeliveryManifest':{'complete':True}})
os._exit(0)
''', [], 10)
