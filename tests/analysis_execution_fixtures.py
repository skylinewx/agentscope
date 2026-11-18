"""Explicit test transport for fixed synthetic scripts; never a production fallback or isolation proof."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

from analysis_extension.execution_resources import ExecutionPolicy


class HostTestBackend:
    def __init__(self, root):
        self.root, self.process = Path(root), None

    async def prepare(self, **kwargs):
        return {'policy': ExecutionPolicy(), 'journalRoot': self.root / 'input-journals',
                'metadata': {'runtime': 'TEST_SUBPROCESS', 'runtimeVersion': 'fixture-v2',
                             'numericPolicyVersion': 'DECIMAL_128_HALF_UP_V2'}}

    async def start(self, plan):
        worker = Path(__file__).resolve().parents[1] / 'examples/agent_service/analysis_extension/isolated_worker.py'
        self.process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-B', str(worker),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        return self.process

    async def cleanup(self, plan):
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        return 'TEST_PROCESS_REMOVED'


def install_test_backend(case, root):
    patcher = patch('analysis_extension.execution_runner.backend_factory', lambda: HostTestBackend(root))
    patcher.start()
    case.addCleanup(patcher.stop)
