"""Recover abandoned host attempts and reclaim only this runtime's labelled containers."""
from contextlib import closing
import hashlib
import json
import sqlite3
import time

from .models import Owner, digest


def input_journal_path(store, owner, run_ref):
    return store.root / 'python-input-journals' / digest(owner.key) / (hashlib.sha256(run_ref.encode()).hexdigest() + '.jsonl')


def remove_archived_journal(store, owner, run_ref):
    """Only call after the durable run contains the host delivery manifest."""
    try:
        input_journal_path(store, owner, run_ref).unlink(missing_ok=True)
    except OSError:
        import logging
        logging.getLogger(__name__).warning('已归档输入台账暂未删除：%s', run_ref)


def recover_expired_runs(store, timestamp):
    with closing(store._connect()) as db:
        pending = db.execute('SELECT owner,ref FROM python_pending WHERE deadline<?', (timestamp,)).fetchall()
    recovered = 0
    for row in pending:
        with store._transaction() as db:
            current = db.execute('SELECT p.digest FROM python_runs p JOIN python_pending q '
                'ON q.owner=p.owner AND q.ref=p.ref WHERE p.owner=? AND p.ref=? AND q.deadline<?',
                (row['owner'], row['ref'], timestamp)).fetchone()
            if current is None:
                continue
            record = store._read(current['digest'])
            if record.get('status') != 'RUNNING':
                continue
            record.update(status='FAILED', businessValidation='NOT_REVIEWED',
                          error={'code':'PYTHON_HOST_INTERRUPTED','message':'宿主中断或执行期限已过；保留冻结输入，可用 retryOfRunRef 重试。'})
            owner = Owner(record['ownerUserId'], record['agentId'], record['sessionId'])
            journal = input_journal_path(store, owner, row['ref'])
            if journal.is_file() and not journal.is_symlink():
                raw = journal.read_bytes()
                events = []
                for line in raw.splitlines():
                    try:
                        events.append(json.loads(line))
                    except (ValueError, UnicodeError):
                        break  # A torn final write is incomplete, never a committed delivery.
                # Preserve interrupted delivery, without manufacturing a completed manifest.
                record['interruptedInputJournal'] = {'sha256': hashlib.sha256(raw).hexdigest(),
                    'byteSize': len(raw), 'status': 'INTERRUPTED',
                    'events': events}
            blob = store._put(db, record)
            db.execute('UPDATE python_runs SET digest=? WHERE owner=? AND ref=?', (blob, row['owner'], row['ref']))
            db.execute('DELETE FROM python_pending WHERE owner=? AND ref=?', (row['owner'], row['ref']))
            recovered += 1
        remove_archived_journal(store, owner, row['ref'])
    return recovered


async def reap_expired_containers():
    from .execution_environment import DockerBackend, docker_cli
    from .execution_resources import ExecutionPolicy, runtime_root
    root = runtime_root()
    database = root / 'admission.sqlite3'
    if not database.is_file():
        return 0
    with closing(sqlite3.connect(database, timeout=2)) as db:
        expired = db.execute("SELECT id,container,state FROM leases WHERE state='CLEANUP_PENDING' OR deadline<?", (time.time(),)).fetchall()
    if not expired:
        return 0
    plan = {'docker':docker_cli(), 'scope':hashlib.sha256(str(root).encode()).hexdigest()[:24],
            'policy':ExecutionPolicy.configured()}
    removed = 0
    for ref, container, state in expired:
        if state == 'QUEUED' or await DockerBackend()._remove({**plan, 'container':container}):
            with closing(sqlite3.connect(database, timeout=2)) as db:
                db.execute('DELETE FROM leases WHERE id=? AND container=?', (ref, container))
                db.commit()
            removed += 1
    return removed
