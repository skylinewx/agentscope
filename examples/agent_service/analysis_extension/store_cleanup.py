"""锁外扫描历史文件，短事务复核快照；有并发写入时让路并稍后重试。"""
from contextlib import closing, contextmanager
from itertools import islice
from pathlib import Path
import sqlite3
import stat
from time import perf_counter

from .models import digest

RETAINED_BLOBS = ('SELECT digest FROM captures UNION SELECT digest FROM analyses '
                  'UNION SELECT digest FROM presentations UNION SELECT digest FROM publication_proposals '
                  'UNION SELECT digest FROM document_checks UNION SELECT digest FROM python_runs '
                  'UNION SELECT digest FROM semantic_reviews UNION SELECT digest FROM native_results')
BATCH_SIZE = 32
BATCH_SECONDS = 0.025


class MaintenanceStopped(Exception):
    pass


def check_stopped(stop):
    if stop is not None and stop.is_set():
        raise MaintenanceStopped()


@contextmanager
def maintenance_lock(root):
    # 独立数据库只负责维护互斥，扫描期间不持有业务库的写锁。
    # SQLite 在进程退出时自动释放锁，无需租约或处理遗留锁文件。
    with closing(sqlite3.connect(Path(root) / 'maintenance.sqlite3', timeout=0)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            yield False
        else:
            try:
                yield True
            finally:
                db.rollback()


def _fingerprint(path):
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _hashed(path, suffix):
    return (path.suffix == suffix and len(path.stem) == 64
            and all(char in '0123456789abcdef' for char in path.stem))


def _markers(store):
    return frozenset(store.root.glob('blob-write-*.pending'))


def scan_blobs(store, retained, cutoff, stop):
    candidates, used = [], 0
    for path in store.blobs.iterdir():
        check_stopped(stop)
        fingerprint = _fingerprint(path)
        if fingerprint is None:
            continue
        used += fingerprint[2]
        if (path.stem not in retained and fingerprint[3] / 1e9 < cutoff
                and (_hashed(path, '.json') or path.name.startswith('pending-'))):
            candidates.append((path, fingerprint, 'blob'))
    return candidates, used


def scan_grids(store, captures, cutoff, stop):
    root = store.root / 'grid-files'
    if not root.is_dir() or root.is_symlink() or root.resolve() != store.root / 'grid-files':
        return []
    candidates = []
    for owner_root in root.iterdir():
        check_stopped(stop)
        if not owner_root.is_dir() or owner_root.is_symlink() or owner_root.resolve().parent != root:
            continue
        for path in owner_root.iterdir():
            check_stopped(stop)
            fingerprint = _fingerprint(path)
            if (fingerprint is not None and fingerprint[3] / 1e9 < cutoff
                    and (_hashed(path, '.jsonl') or path.name.startswith('grid-') and path.suffix == '.tmp')):
                candidates.append((path, fingerprint, 'grid'))
    # 没有旧网格文件时，无须逐个读取所有历史回执。
    if not candidates:
        return []
    retained = set()
    for owner, blob in captures:
        check_stopped(stop)
        capture = store._read(blob)
        retained.update((digest(owner), value['sha256']) for value in capture.get('gridFiles', []))
    return [item for item in candidates if (item[0].parent.name, item[0].stem) not in retained]


def collect_expired(store, db, cutoff, stop):
    check_stopped(stop)
    with store._transaction(connection=db):
        deleted = db.execute('DELETE FROM captures WHERE created<? AND ref NOT IN (SELECT capture FROM pins)', (cutoff,)).rowcount
        db.execute('DELETE FROM semantic_reviews WHERE created<?', (cutoff,))
        db.execute('DELETE FROM native_results WHERE created<?', (cutoff,))
        db.execute('DELETE FROM turn_progress WHERE updated<?', (cutoff,))
        retained = {row[0] for row in db.execute(RETAINED_BLOBS)}
        captures = [tuple(row) for row in db.execute('SELECT owner,digest FROM captures')]
        version = db.execute('PRAGMA data_version').fetchone()[0]
        markers = _markers(store)
    # 不持有读/写事务：扫描慢磁盘、解析大回执时，新提问仍能读写业务库。
    blobs, used = scan_blobs(store, retained, cutoff, stop)
    grids = scan_grids(store, captures, cutoff, stop)
    candidates = iter(blobs + grids)
    result = {'capturesRemoved': deleted, 'blobsRemoved': 0, 'gridFilesRemoved': 0}
    quota_saved = False
    while True:
        check_stopped(stop)
        with store._transaction(connection=db):
            # 同一个连接的 data_version 只因其他连接提交而变化。
            # 回滚可能留下文件，故还要复核写入标记；快照过期就保守延期。
            if db.execute('PRAGMA data_version').fetchone()[0] != version or _markers(store) != markers:
                return {**result, 'deferred': True}
            if not quota_saved:
                db.execute('INSERT INTO blob_usage VALUES(1,?) ON CONFLICT(id) DO UPDATE SET bytes=excluded.bytes', (used,))
                if markers:
                    store._mark_blob_write(db)
                    for marker in markers:
                        marker.unlink(missing_ok=True)
                quota_saved = True
                markers = frozenset()
            started = perf_counter()
            exhausted = True
            for path, fingerprint, kind in islice(candidates, BATCH_SIZE):
                check_stopped(stop)
                exhausted = False
                # 文件复用会刷新 mtime；目录也必须仍属于专用存储，不能跟随符号链接。
                parent = store.blobs if kind == 'blob' else store.root / 'grid-files' / path.parent.name
                if path.parent.resolve() == parent and _fingerprint(path) == fingerprint:
                    if kind == 'blob':
                        store._mark_blob_write(db)
                    path.unlink()
                    if kind == 'blob':
                        db.execute('UPDATE blob_usage SET bytes=bytes-? WHERE id=1', (fingerprint[2],))
                        db.execute('DELETE FROM blobs WHERE digest=?', (path.stem,))
                    result['blobsRemoved' if kind == 'blob' else 'gridFilesRemoved'] += 1
                if perf_counter() - started >= BATCH_SECONDS:
                    break
        if exhausted:
            return result
