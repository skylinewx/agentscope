"""Deployment policy and cross-process admission on one Docker host."""
import asyncio
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .models import AnalysisError

GIB = 1024 ** 3
PYTHON_DEFAULT_TIMEOUT_SECONDS = 60
PYTHON_MAX_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class ExecutionPolicy:
    memory_bytes: int = 2 * GIB
    cpus: float = 2
    memory_budget: int = 8 * GIB
    cpu_budget: float = 6
    max_parallel: int = 3
    queue_capacity: int = 32
    session_parallel: int = 1
    queue_seconds: float = 300
    prepare_seconds: float = 60
    cleanup_seconds: float = 10
    log_bytes: int = 1024 * 1024
    tmp_bytes: int = 64 * 1024 * 1024
    pids: int = 64
    library_threads: int = 1

    @classmethod
    def configured(cls):
        defaults = asdict(cls())
        values = {}
        try:
            for key, default in defaults.items():
                raw = os.environ.get('ANALYSIS_PYTHON_' + key.upper(), str(default))
                values[key] = float(raw) if key in ('cpus', 'cpu_budget') or key.endswith('_seconds') else int(raw)
            policy = cls(**values)
            if (any(not math.isfinite(value) or value <= 0 for value in values.values()) or policy.memory_bytes < 64 * 1024 * 1024
                    or policy.memory_bytes > policy.memory_budget or policy.cpus > policy.cpu_budget
                    or policy.library_threads > max(1, int(policy.cpus))):
                raise ValueError('任务额度超过总预算或资源参数不合法')
        except (TypeError, ValueError, OverflowError) as error:
            raise AnalysisError('PYTHON_RESOURCE_CONFIG', str(error)) from error
        return policy


def runtime_root():
    configured = os.environ.get('ANALYSIS_RUNTIME_ROOT')
    root = Path(configured) if configured else Path(__file__).resolve().parents[1] / 'data' / 'analysis-runtime'
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


class Admission:
    def __init__(self, policy, capacity, root=None):
        self.policy = policy
        self.root = Path(root) if root else runtime_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'admission.sqlite3'
        self.memory_budget = min(policy.memory_budget, max(0, capacity.get('MemTotal', policy.memory_budget + 2 * GIB) - 2 * GIB))
        self.cpu_budget = min(policy.cpu_budget, capacity.get('NCPU', policy.cpu_budget))
        if policy.memory_bytes > self.memory_budget or policy.cpus > self.cpu_budget:
            raise AnalysisError('PYTHON_RESOURCE_CONFIG', 'Docker 可用资源不足以容纳配置的单任务额度。')
        with closing(self.connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, session TEXT NOT NULL, pid INTEGER NOT NULL, '
                       'created REAL NOT NULL, state TEXT NOT NULL, memory INTEGER NOT NULL, cpus REAL NOT NULL, '
                       'container TEXT NOT NULL, deadline REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            configured = db.execute('SELECT fingerprint FROM policy WHERE id=1').fetchone()
            if configured and configured[0] != self.identity() and db.execute('SELECT 1 FROM leases LIMIT 1').fetchone():
                raise AnalysisError('PYTHON_RESOURCE_CONFIG', '同一宿主的执行 worker 必须使用一致的资源配置。')
            db.execute('INSERT INTO policy VALUES(1,?) ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint', (self.identity(),))
            db.commit()

    def connect(self):
        return sqlite3.connect(self.database, timeout=2)

    def enqueue(self, ref, session, container, deadline):
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT COUNT(*) FROM leases WHERE state='QUEUED'").fetchone()[0] >= self.policy.queue_capacity:
                raise AnalysisError('PYTHON_QUEUE_FULL', '分析执行队列已满；原输入保留，请稍后重试。')
            db.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?)', (ref, session, os.getpid(), time.time(),
                'QUEUED', self.policy.memory_bytes, self.policy.cpus, container, deadline))
            db.commit()

    def try_start(self, ref):
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT session,created FROM leases WHERE id=?', (ref,)).fetchone()
            if not current:
                raise AnalysisError('PYTHON_LEASE_LOST', '执行额度记录丢失，不能启动任务。')
            count, memory, cpus = db.execute("SELECT COUNT(*),COALESCE(SUM(memory),0),COALESCE(SUM(cpus),0) "
                                            "FROM leases WHERE state!='QUEUED'").fetchone()
            same = db.execute("SELECT COUNT(*) FROM leases WHERE session=? AND state!='QUEUED'", (current[0],)).fetchone()[0]
            earlier = db.execute("SELECT 1 FROM leases q WHERE q.state='QUEUED' AND q.created<? AND NOT EXISTS "
                "(SELECT 1 FROM leases r WHERE r.session=q.session AND r.state!='QUEUED') LIMIT 1", (current[1],)).fetchone()
            if (earlier or count >= self.policy.max_parallel or same >= self.policy.session_parallel
                    or memory + self.policy.memory_bytes > self.memory_budget or cpus + self.policy.cpus > self.cpu_budget):
                return False
            db.execute("UPDATE leases SET state='RUNNING' WHERE id=?", (ref,))
            db.commit()
            return True

    async def acquire(self, ref, session, container, execution_seconds):
        deadline = time.time() + self.policy.queue_seconds + self.policy.prepare_seconds + execution_seconds + self.policy.cleanup_seconds
        pending = []
        async def call(function, *args):
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            pending.append(task)
            return await asyncio.shield(task)
        start = time.monotonic()
        try:
            await call(self.enqueue, ref, session, container, deadline)
            while not await call(self.try_start, ref):
                if time.monotonic() - start >= self.policy.queue_seconds:
                    raise AnalysisError('PYTHON_QUEUE_TIMEOUT', '等待分析执行额度超时；原输入保留，未执行计算。')
                await asyncio.sleep(0.05)
        except BaseException:
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.to_thread(self.release, ref, True)
            raise
        return round((time.monotonic() - start) * 1000, 2)

    def release(self, ref, cleaned):
        with closing(self.connect()) as db:
            if cleaned:
                db.execute('DELETE FROM leases WHERE id=?', (ref,))
            else:
                db.execute("UPDATE leases SET state='CLEANUP_PENDING' WHERE id=?", (ref,))
            db.commit()

    def expired(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT id,container,state FROM leases WHERE state='CLEANUP_PENDING' OR deadline<?", (time.time(),)).fetchall()

    def identity(self):
        return hashlib.sha256(json.dumps(asdict(self.policy), sort_keys=True).encode()).hexdigest()
