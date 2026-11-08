"""服务生命周期内的后台维护；会话装配不触发或等待清理。"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from time import perf_counter

from agentscope._logging import logger

from .activation import load_config
from .models import Limits
from .store import AnalysisStore


def run_maintenance(config_path, stop, poll_seconds=30):
    root = Path(config_path).resolve().parent / 'analysis-data'
    while not stop.is_set():
        try:
            config = load_config(config_path)
            capabilities = config.get('capabilities', {})
            enabled = config.get('activation', 'auto') != 'disabled' and any(
                capabilities.get(name, True) for name in ('analysis', 'reports'))
            # 空服务不创建数据库；首个会话创建后，下次巡检即可维护。
            if enabled and root.is_dir():
                started = perf_counter()
                result = AnalysisStore(root, Limits(**config.get('limits', {}))).collect_expired_if_due(stop=stop)
                if result.get('performed'):
                    logger.info('分析存储后台维护 耗时毫秒=%.2f 结果=%s',
                                (perf_counter() - started) * 1000, result)
        except Exception:
            # 维护失败可以重试，不影响会话接收、模型调用或答复。
            logger.exception('分析存储后台维护失败，将在下次巡检重试')
        stop.wait(poll_seconds)


def maintenance_lifespan(config_path):
    @asynccontextmanager
    async def lifespan(app):
        stop = Event()
        task = asyncio.create_task(asyncio.to_thread(run_maintenance, config_path, stop),
                                   name='analysis-storage-maintenance')
        try:
            yield
        finally:
            # to_thread 的取消不会停止线程：通知扫描协作退出，并等它释放数据库。
            stop.set()
            await asyncio.shield(task)
    return lifespan
