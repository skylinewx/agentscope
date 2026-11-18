"""固定分析镜像的准备、复用及逐次隔离执行，不允许脚本安装任意依赖。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4
from weakref import WeakKeyDictionary

from .models import AnalysisError
from .python_libraries import NUMERIC_POLICY, library_imports


ROOT = Path(__file__).resolve().parent
ENVIRONMENT = ROOT / 'python-environment'
MEMORY_BYTES = 1024 * 1024 * 1024
_ready = {}
_locks = WeakKeyDictionary()


def fingerprint(libraries=False):
    """依赖与执行策略进入环境标识；原请求重试仍返回原运行，不偷换环境重算。"""
    parts = [(ROOT / name).read_bytes() for name in ('python_worker.py', 'python_libraries.py', 'python_environment.py')]
    if libraries:
        parts += [(ENVIRONMENT / name).read_bytes() for name in ('Dockerfile', 'requirements.lock')]
    else:
        parts += [sys.version.encode(), sys.platform.encode()]
    return hashlib.sha256(b'\0'.join(parts + [NUMERIC_POLICY.encode()])).hexdigest()


def image_fingerprint():
    """库镜像仅随依赖变化；修改分析代码不触发重新安装科学计算库。"""
    return hashlib.sha256(b'\0'.join((ENVIRONMENT / name).read_bytes()
                                   for name in ('Dockerfile', 'requirements.lock'))).hexdigest()


def docker_cli():
    command = shutil.which('docker')
    if not command and os.name == 'nt':
        candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DockerDesktop/resources/bin/docker.exe'
        if candidate.is_file():
            command = str(candidate)
    if not command:
        raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '分析库需要 Docker 执行环境；未回退到宿主开放执行。')
    return command


async def command_output(arguments, timeout=60):
    process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    return process.returncode, output.decode('utf-8', errors='replace')


async def prepare():
    """每种锁定环境只准备一次；允许部署预热，后续问题复用同一只读镜像。"""
    identity = image_fingerprint()
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        if identity in _ready:
            return _ready[identity]
        cli = docker_cli()
        tag = 'agentscope-analysis:' + identity[:16]
        code, output = await command_output([cli, 'image', 'inspect', tag, '--format', '{{.Id}}'])
        if code:
            code, output = await command_output([cli, 'build', '--label', 'agentscope.analysis=' + identity,
                '--tag', tag, str(ENVIRONMENT)], timeout=600)
            if code:
                raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '锁定的分析环境准备失败。',
                                    {'environmentId': identity, 'diagnostic': output[-2000:]})
            code, output = await command_output([cli, 'image', 'inspect', tag, '--format', '{{.Id}}'])
        if code or not output.strip().startswith('sha256:'):
            raise AnalysisError('PYTHON_ENVIRONMENT_UNAVAILABLE', '分析环境镜像无法确认。')
        _ready[identity] = {'environmentId': identity, 'imageId': output.strip(), 'docker': cli}
        return _ready[identity]


async def execution_plan(tree, inputs):
    """容器只读挂载运行代码与本次授权网格，不挂载工作区、凭据或 Docker socket。"""
    libraries = library_imports(tree)
    if not libraries:
        return {'command': [sys.executable, '-I', '-S', str(ROOT / 'python_worker.py')], 'inputs': inputs,
                'metadata': {'environmentId': fingerprint(), 'runtime': 'RESTRICTED_PYTHON',
                             'libraries': [], 'memoryLimitBytes': 256 * 1024 * 1024}}
    environment = await prepare()
    name = 'analysis-' + uuid4().hex
    # Linux 网格为服务身份的 0600 文件，沿用该 UID/GID 才能只读挂载。
    # Windows bind mount 无此 POSIX 身份约束，使用镜像的非 root 身份。
    user = f'{os.getuid()}:{os.getgid()}' if os.name != 'nt' else '65534:65534'
    arguments = [environment['docker'], 'run', '--rm', '-i', '--name', name, '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', user,
        '--memory', str(MEMORY_BYTES), '--memory-swap', str(MEMORY_BYTES), '--cpus', '2', '--pids-limit', '64',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=67108864', '--workdir', '/tmp',
        '--mount', 'type=bind,source=' + str(ROOT) + ',target=/opt/analysis-worker,readonly']
    for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        arguments += ['--env', key + '=1']
    arguments += ['--env', 'ANALYSIS_LIBRARY_WORKER=1']
    copied = json.loads(json.dumps(inputs))
    for index, value in enumerate(copied):
        if value.get('kind') != 'GRID_FILE':
            continue
        source = Path(value['_path']).resolve(strict=True)
        target = '/inputs/grid-' + str(index) + '.jsonl'
        arguments += ['--mount', f'type=bind,source={source},target={target},readonly']
        value['_path'] = target
    arguments += [environment['imageId'], 'python', '-I', '-B', '/opt/analysis-worker/python_worker.py']
    return {'command': arguments, 'inputs': copied, 'container': name, 'docker': environment['docker'],
            'metadata': {'environmentId': fingerprint(True), 'packageEnvironmentId': environment['environmentId'],
                         'imageId': environment['imageId'],
                         'runtime': 'ISOLATED_ANALYSIS_LIBRARIES', 'libraries': libraries, 'memoryLimitBytes': MEMORY_BYTES}}


async def cleanup(plan):
    if plan.get('container'):
        await command_output([plan['docker'], 'rm', '--force', plan['container']], timeout=15)


if __name__ == '__main__':
    print(json.dumps(asyncio.run(prepare()), ensure_ascii=False))
