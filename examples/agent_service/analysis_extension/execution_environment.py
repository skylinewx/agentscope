"""One immutable container backend for every AnalysisPython job; no host fallback."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from .execution_resources import ExecutionPolicy, Admission, runtime_root
from .models import AnalysisError
from .numeric_policy import NUMERIC_POLICY

ROOT = Path(__file__).resolve().parent
ENVIRONMENT = ROOT / 'python-environment'
WORKER_FILES = ('isolated_worker.py', 'python_program.py', 'python_sdk.py', 'execution_protocol.py', 'numeric_policy.py')
_ready = {}


def image_fingerprint():
    contents = [(ROOT / name).read_bytes() for name in WORKER_FILES]
    contents += [(ENVIRONMENT / name).read_bytes() for name in ('Dockerfile', 'requirements.lock')]
    return hashlib.sha256(b'\0'.join(contents)).hexdigest()


def docker_cli():
    command = shutil.which('docker')
    if not command and os.name == 'nt':
        path = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DockerDesktop/resources/bin/docker.exe'
        if path.is_file():
            command = str(path)
    if not command:
        raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', 'Python 分析需要已准备的 Docker 环境；不会回退到宿主执行。')
    return command


async def command_output(arguments, timeout=60):
    process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
    tail = bytearray()

    async def collect():
        while chunk := await process.stdout.read(16384):
            tail.extend(chunk)
            if len(tail) > 65536:
                del tail[:-65536]
        await process.wait()

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(task, timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return process.returncode, tail.decode('utf-8', errors='replace')


async def prepare(build=False):
    identity = image_fingerprint()
    if identity in _ready and not build:
        return _ready[identity]
    cli, tag = docker_cli(), 'agentscope-analysis:' + identity[:16]
    code, output = await command_output([cli, 'image', 'inspect', tag, '--format', '{{.Id}}'])
    if code and build:
        with tempfile.TemporaryDirectory(prefix='image-', dir=runtime_root()) as directory:
            context = Path(directory)
            for name in ('requirements.lock',):
                shutil.copyfile(ENVIRONMENT / name, context / name)
            dockerfile = (ENVIRONMENT / 'Dockerfile').read_text(encoding='utf-8')
            dockerfile += '\nCOPY ' + ' '.join(WORKER_FILES) + ' /opt/analysis-worker/\n'
            (context / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
            for name in WORKER_FILES:
                shutil.copyfile(ROOT / name, context / name)
            code, output = await command_output([cli, 'build', '--label', 'agentscope.analysis=' + identity,
                '--tag', tag, str(context)], timeout=600)
            if code:
                raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '锁定分析镜像准备失败。', {'diagnostic': output[-2000:]})
        code, output = await command_output([cli, 'image', 'inspect', tag, '--format', '{{.Id}}'])
    if code or not output.strip().startswith('sha256:'):
        raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '分析镜像尚未准备；先运行部署准备命令，查询中不自动安装。',
            {'environmentId': identity, 'prepareCommand': 'python -m analysis_extension.execution_environment --prepare'})
    image_id = output.strip()
    code, output = await command_output([cli, 'info', '--format', '{{json .}}'])
    if code:
        raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', 'Docker 引擎不可用。', {'diagnostic': output[-1200:]})
    info = json.loads(output)
    result = {'environmentId': identity, 'imageId': image_id, 'docker': cli,
              'capacity': {key: info[key] for key in ('NCPU', 'MemTotal')}}
    _ready[identity] = result
    return result


class DockerBackend:
    async def prepare(self, *, session, run_ref, timeout_seconds):
        policy = ExecutionPolicy.configured()
        try:
            environment = await asyncio.wait_for(prepare(), policy.prepare_seconds)
        except (OSError, asyncio.TimeoutError, ValueError) as error:
            raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '分析环境准备检查失败，原输入保留。') from error
        gate = Admission(policy, environment['capacity'])
        scope = hashlib.sha256(str(gate.root).encode()).hexdigest()[:24]
        plan = {'docker': environment['docker'], 'imageId': environment['imageId'],
            'container': 'analysis-' + uuid4().hex, 'scope': scope, 'policy': policy,
            'gate': gate, 'runRef': run_ref, 'journalRoot': gate.root / 'journals',
            'metadata': {'runtime': 'ISOLATED_PYTHON', 'runtimeVersion': environment['environmentId'],
                'environmentId': environment['environmentId'], 'imageId': environment['imageId'],
                'numericPolicyVersion': NUMERIC_POLICY, 'resourcePolicy': asdict(policy),
                'effectiveCapacity': {'memoryBudget': gate.memory_budget, 'cpuBudget': gate.cpu_budget},
                'memoryLimitBytes': policy.memory_bytes}}
        for ref, container, state in await asyncio.to_thread(gate.expired):
            cleaned = state == 'QUEUED' or await self._remove({**plan, 'container': container})
            await asyncio.to_thread(gate.release, ref, cleaned)
        plan['metadata']['queueMs'] = await gate.acquire(run_ref, session, plan['container'], timeout_seconds)
        return plan

    async def start(self, plan):
        policy = plan['policy']
        args = [plan['docker'], 'create', '--rm', '-i', '--name', plan['container'],
            '--label', 'agentscope.analysis.runtime=' + plan['scope'], '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', '65534:65534',
            '--memory', str(policy.memory_bytes), '--memory-swap', str(policy.memory_bytes),
            '--cpus', str(policy.cpus), '--pids-limit', str(policy.pids),
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=' + str(policy.tmp_bytes), '--workdir', '/tmp']
        for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            args.extend(['--env', name + '=' + str(policy.library_threads)])
        args.extend([plan['imageId'], 'python', '-I', '-B', '/opt/analysis-worker/isolated_worker.py'])
        code, output = await command_output(args, policy.prepare_seconds)
        if code:
            raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '无法创建分析容器。', {'diagnostic': output[-1200:]})
        return await asyncio.create_subprocess_exec(plan['docker'], 'start', '-a', '-i', plan['container'],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}))

    async def _remove(self, plan):
        try:
            code, output = await command_output([plan['docker'], 'inspect', plan['container'],
                '--format', '{{json .Config.Labels}}'], plan['policy'].cleanup_seconds)
            if code:
                return 'no such object' in output.lower() or 'no such container' in output.lower()
            if json.loads(output).get('agentscope.analysis.runtime') != plan['scope']:
                return False
            code, _ = await command_output([plan['docker'], 'rm', '--force', plan['container']], plan['policy'].cleanup_seconds)
            if code == 0:
                return True
            # --rm may remove the container between inspect and explicit cleanup.
            code, output = await command_output([plan['docker'], 'inspect', plan['container'],
                '--format', '{{.State.Running}}'], plan['policy'].cleanup_seconds)
            return code != 0 and ('no such object' in output.lower() or 'no such container' in output.lower())
        except (OSError, asyncio.TimeoutError, ValueError):
            return False

    async def cleanup(self, plan):
        cleaned = await self._remove(plan)
        await asyncio.to_thread(plan['gate'].release, plan['runRef'], cleaned)
        return 'REMOVED' if cleaned else 'CLEANUP_PENDING'


if __name__ == '__main__':
    import sys
    print(json.dumps(asyncio.run(prepare(build='--prepare' in sys.argv)), ensure_ascii=False))
