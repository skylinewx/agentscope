"""Single-host immutable blobs and transactional ownership/version registry."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing, contextmanager

from .models import AnalysisError, Limits, Owner, canonical, digest, now
from .protocol import validate_artifact

HOST_ID = uuid.uuid4().hex  # Single-process host lifetime, never a model parameter.


class AnalysisStore:
    def __init__(self, root: str | Path, limits: Limits | None = None):
        self.root = Path(root).resolve()
        self.limits = limits or Limits()
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "analysis.sqlite3"
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS blobs(digest TEXT PRIMARY KEY, size INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS captures(ref TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS captures_owner ON captures(owner, created);
                CREATE TABLE IF NOT EXISTS analyses(ref TEXT PRIMARY KEY, owner TEXT NOT NULL, aid TEXT NOT NULL,
                    revision INTEGER NOT NULL, base INTEGER NOT NULL, state TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS analyses_owner ON analyses(owner, aid, revision);
                CREATE TABLE IF NOT EXISTS heads(owner TEXT NOT NULL, aid TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(owner, aid));
                CREATE TABLE IF NOT EXISTS requests(owner TEXT NOT NULL, phase TEXT NOT NULL, key TEXT NOT NULL,
                    digest TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(owner, phase, key));
                CREATE TABLE IF NOT EXISTS pins(analysis TEXT NOT NULL, capture TEXT NOT NULL, PRIMARY KEY(analysis, capture));
                CREATE TABLE IF NOT EXISTS presentations(owner TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
                    analysis TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(owner,kind,ref));
                CREATE TABLE IF NOT EXISTS publication_proposals(ref TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    analysis TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS document_checks(owner TEXT NOT NULL, analysis TEXT NOT NULL,
                    content TEXT NOT NULL, width INTEGER NOT NULL, digest TEXT NOT NULL, checked REAL NOT NULL,
                    PRIMARY KEY(owner,analysis,content,width));
                CREATE TABLE IF NOT EXISTS action_runs(ref TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    action TEXT NOT NULL, origin TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS action_runs_owner ON action_runs(owner,origin);
                CREATE TABLE IF NOT EXISTS action_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL, code TEXT NOT NULL, at TEXT NOT NULL);
            """)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _blob_path(self, content_digest: str) -> Path:
        if len(content_digest) != 64 or any(c not in "0123456789abcdef" for c in content_digest):
            raise AnalysisError("CORRUPT_RECORD", "分析内容登记异常。")
        path = self.blobs / (content_digest + ".json")
        if path.resolve().parent != self.blobs.resolve() or path.is_symlink():
            raise AnalysisError("UNSAFE_PATH", "分析内容路径不属于存储目录。")
        return path

    def _put(self, db, data: dict) -> str:
        content = canonical(data)
        if len(content) > self.limits.max_receipt_bytes:
            raise AnalysisError("INPUT_TOO_LARGE", "分析输入超过配置上限；未保存截断内容。")
        content_digest = hashlib.sha256(content).hexdigest()
        path = self._blob_path(content_digest)
        if path.exists():
            if path.read_bytes() != content:
                raise AnalysisError("DIGEST_MISMATCH", "已保存的分析内容被修改。")
        else:
            # Include unregistered files left by a crash in quota accounting.
            used = sum(p.stat().st_size for p in self.blobs.iterdir() if p.is_file())
            if used + len(content) > self.limits.max_store_bytes:
                raise AnalysisError("STORE_FULL", "分析存储达到配额，已发布依据仍保留。")
            fd, temporary = tempfile.mkstemp(prefix="pending-", dir=self.blobs)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (content_digest, len(content), time.time()))
        return content_digest

    def _read(self, content_digest: str) -> dict:
        try:
            content = self._blob_path(content_digest).read_bytes()
        except FileNotFoundError as exc:
            raise AnalysisError("MISSING_CONTENT", "已登记的分析内容不可用。") from exc
        if len(content) > self.limits.max_receipt_bytes or hashlib.sha256(content).hexdigest() != content_digest:
            raise AnalysisError("DIGEST_MISMATCH", "分析内容与登记摘要不一致。")
        return json.loads(content)

    @staticmethod
    def _request(db, owner, phase, key, request_digest):
        if not isinstance(key, str) or not key or len(key) > 256:
            raise AnalysisError("INVALID_REQUEST_KEY", "需要非空且有界的幂等请求标识。")
        row = db.execute("SELECT * FROM requests WHERE owner=? AND phase=? AND key=?", (owner.key, phase, key)).fetchone()
        if row and row["digest"] != request_digest:
            raise AnalysisError("REQUEST_CONFLICT", "同一个请求标识不能用于不同的分析内容。")
        return row["ref"] if row else None

    def capture(self, owner: Owner, data: dict) -> str:
        payload = {**data, **owner.fields(), "captureEncoding": "tool-response-json/utf-8"}
        ref = "cap_" + digest({"owner": owner.key, "payload": payload})
        payload["captureRef"] = ref
        with self._transaction() as db:
            row = db.execute("SELECT digest FROM captures WHERE ref=? AND owner=?", (ref, owner.key)).fetchone()
            if row:
                self._read(row["digest"])
                return ref
            blob = self._put(db, payload)
            db.execute("INSERT INTO captures VALUES(?,?,?,?)", (ref, owner.key, blob, time.time()))
        return ref

    def find_request(self, owner, phase, key, request_digest):
        with closing(self._connect()) as db:
            return self._request(db, owner, phase, key, request_digest)

    def get_capture(self, owner: Owner, ref: str) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT digest FROM captures WHERE ref=? AND owner=?", (ref, owner.key)).fetchone()
        if not row:
            raise AnalysisError("NOT_FOUND", "当前会话没有该输入引用。")
        return self._read(row["digest"])

    def list_captures(self, owner: Owner, offset=0, limit=100) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= self.limits.page_size:
            raise AnalysisError("INVALID_PAGE", "分页参数超出范围。")
        with closing(self._connect()) as db:
            total = db.execute("SELECT COUNT(*) FROM captures WHERE owner=?", (owner.key,)).fetchone()[0]
            rows = db.execute("SELECT ref,digest FROM captures WHERE owner=? ORDER BY created,ref LIMIT ? OFFSET ?", (owner.key, limit, offset)).fetchall()
        items = []
        for row in rows:
            value = self._read(row["digest"])
            items.append({k: value.get(k) for k in ("captureRef", "toolName", "originalToolName", "capturedAt", "captureState", "receiptSummary")})
        return {"items": items, "total": total, "nextOffset": offset + len(items) if offset + len(items) < total else None}

    def list_results(self, owner: Owner, offset: int, limit: int) -> dict:
        from .models import bounded_page
        bounded_page([], offset, limit, self.limits.page_size)
        with closing(self._connect()) as db:
            total = db.execute('SELECT COUNT(*) FROM analyses WHERE owner=?', (owner.key,)).fetchone()[0]
            rows = db.execute('SELECT ref FROM analyses WHERE owner=? ORDER BY rowid LIMIT ? OFFSET ?',
                              (owner.key, limit, offset)).fetchall()
        items = []
        for row in rows:
            artifact = self.get_analysis(owner, row['ref'])
            scope = artifact['scope']
            items.append({'analysisRef': artifact['analysisRef'], 'publicationState': artifact['publicationState'],
                          'label': scope.get('label'), 'selectionReason': scope.get('selectionReason'),
                          'createdAt': artifact['createdAt'], 'memberCount': len(scope.get('members', [])),
                          'factCount': len(artifact['facts']), 'reportTopics': [
                              {k:t[k] for k in ('title','question')} for t in scope.get('reportTopics', [])],
                          'nextSection': 'summary'})
        return {'items': items, 'total': total, 'offset': offset,
                'nextOffset': offset+len(items) if offset+len(items)<total else None}

    def get_analysis(self, owner: Owner, ref: str) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT digest FROM analyses WHERE ref=? AND owner=?", (ref, owner.key)).fetchone()
        if not row:
            raise AnalysisError("NOT_FOUND", "当前会话没有该分析引用。")
        artifact = self._read(row["digest"])
        validate_artifact(artifact)
        return artifact

    def build(self, owner: Owner, request: dict, artifact: dict, key: str, base_ref: str | None) -> dict:
        request_digest = digest(request)
        with self._transaction() as db:
            reused = self._request(db, owner, "build", key, request_digest)
            if reused:
                return self.get_analysis(owner, reused)
            if base_ref:
                base = db.execute("SELECT * FROM analyses WHERE owner=? AND ref=?", (owner.key, base_ref)).fetchone()
                if not base:
                    raise AnalysisError("INVALID_BASE", "重建必须引用本会话实际存在的分析草稿或已发布版本。")
                validate_artifact(self._read(base["digest"]))
                aid = base['aid']
                base_revision = base['revision'] if base['state'] == 'PUBLISHED' else base['base']
                revision = db.execute("SELECT MAX(revision)+1 FROM analyses WHERE owner=? AND aid=?", (owner.key, aid)).fetchone()[0]
            else:
                aid, base_revision, revision = "ana_" + uuid.uuid4().hex, 0, 1
            ref = f"{aid}:{revision}"
            result = {**artifact, **owner.fields(), "analysisId": aid, "analysisRef": ref,
                      "revision": revision, "baseRevision": base_revision, "createdAt": now(), "publicationState": "DRAFT"}
            validate_artifact(result)
            blob = self._put(db, result)
            for capture_ref in result["captureRefs"]:
                if not db.execute("SELECT 1 FROM captures WHERE ref=? AND owner=?", (capture_ref, owner.key)).fetchone():
                    raise AnalysisError("NOT_FOUND", "分析引用了本会话以外的输入。")
                db.execute("INSERT INTO pins VALUES(?,?)", (ref, capture_ref))
            db.execute("INSERT INTO analyses VALUES(?,?,?,?,?,?,?)", (ref, owner.key, aid, revision, base_revision, "DRAFT", blob))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?)", (owner.key, "build", key, request_digest, ref))
        return result

    def publish(self, owner: Owner, ref: str, result: dict, request: dict, key: str, expected_base: int) -> dict:
        request_digest = digest(request)
        with self._transaction() as db:
            reused = self._request(db, owner, "publish", key, request_digest)
            if reused:
                return self.get_analysis(owner, reused)
            row = db.execute("SELECT * FROM analyses WHERE ref=? AND owner=?", (ref, owner.key)).fetchone()
            if not row:
                raise AnalysisError("NOT_FOUND", "当前会话没有该分析引用。")
            if row["state"] != "DRAFT":
                raise AnalysisError("IMMUTABLE_VERSION", "已发布版本不可覆盖，请使用原请求重试或创建新版本。")
            head = db.execute("SELECT revision FROM heads WHERE owner=? AND aid=?", (owner.key, row["aid"])).fetchone()
            current = head[0] if head else 0
            if type(expected_base) is not int or expected_base != row["base"] or current != expected_base:
                raise AnalysisError("VERSION_CONFLICT", "基础版本已变化，请重新创建分析版本。")
            # Do not trust a caller to change identity or numerical facts on publish.
            original = self._read(row["digest"])
            for field in set(original) - {"claims", "viewPlan", "findings", "actions", "technicalDetails"}:
                if original[field] != result.get(field):
                    raise AnalysisError("IMMUTABLE_FACTS", "发布不能改变已构建的事实和范围。")
            for capture_ref in original["captureRefs"]:
                self.get_capture(owner, capture_ref)
            result = {**result, "publicationState": "PUBLISHED", "publishedAt": now()}
            validate_artifact(result)
            # Reject content that no supported card/report reader can render
            # before committing either publication or an action result.
            from .actions import projection
            projection(result)
            blob = self._put(db, result)
            db.execute("UPDATE analyses SET state='PUBLISHED',digest=? WHERE ref=?", (blob, ref))
            db.execute("INSERT INTO heads VALUES(?,?,?) ON CONFLICT(owner,aid) DO UPDATE SET revision=excluded.revision", (owner.key, row["aid"], row["revision"]))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?)", (owner.key, "publish", key, request_digest, ref))
            if result.get("actionRunRef"):
                from .actions import outcome, resolve
                run = self._run(db, owner, result["actionRunRef"])
                if run["status"] != "RUNNING" or run.get("resultAnalysisRef"):
                    raise AnalysisError("ACTION_NOT_RUNNING", "行动已结束或已关联结果，不能重复发布。")
                origin, action = resolve(self, owner, run["actionRef"])
                state, code = outcome(result, action, origin)
                run["resultAnalysisRef"] = ref
                self._write_run(db, owner, run, state, code)
        return result

    def _run(self, db, owner, ref):
        row = db.execute("SELECT data FROM action_runs WHERE owner=? AND ref=?", (owner.key, ref)).fetchone()
        if not row:
            raise AnalysisError("NOT_FOUND", "本会话没有该追查记录。")
        return json.loads(row[0])

    def _write_run(self, db, owner, run, status, code):
        from .actions import RUN_STATES
        if status not in RUN_STATES:
            raise AnalysisError("INVALID_ACTION_STATE", "未知的追查状态。")
        if status == "COMPLETED" and not db.execute("SELECT 1 FROM analyses WHERE owner=? AND ref=? AND state='PUBLISHED'", (owner.key, run.get("resultAnalysisRef"))).fetchone():
            raise AnalysisError("ACTION_RESULT_REQUIRED", "行动完成必须关联本会话已发布的结果。")
        run["status"], run["updatedAt"] = status, now()
        run["diagnostics"] = (run.get("diagnostics", []) + [{"code": code, "at": run["updatedAt"]}])[-30:]
        db.execute("UPDATE action_runs SET data=? WHERE ref=? AND owner=?", (canonical(run).decode(), run["actionRunId"], owner.key))
        db.execute("INSERT INTO action_events(run,owner,status,code,at) VALUES(?,?,?,?,?)", (run["actionRunId"], owner.key, status, code, run["updatedAt"]))
        return run

    def start_action_run(self, owner, action, key, turn_ref, runnable):
        identity = digest({"actionRef": action["actionRef"], "goal": action["goal"], "policy": action["policy"]})
        with self._transaction() as db:
            old = self._request(db, owner, "followup", key, identity)
            if old:
                return self._run(db, owner, old)
            previous = db.execute("SELECT data FROM action_runs WHERE owner=? AND action=? ORDER BY rowid DESC LIMIT 1", (owner.key, action["actionRef"])).fetchone()
            if previous and json.loads(previous[0])["turnRef"] == turn_ref:
                raise AnalysisError("NO_NEW_EVIDENCE", "同一回合已登记该行动，请复用原 requestKey，不重复启动。")
            if db.execute("SELECT COUNT(*) FROM action_runs WHERE owner=?", (owner.key,)).fetchone()[0] >= 1000:
                raise AnalysisError("ACTION_LIMIT", "当前会话追查记录已达上限，历史结果继续保留。")
            run = {"schemaVersion": 1, "actionRunId": "run_" + uuid.uuid4().hex,
                   "actionRef": action["actionRef"], "originAnalysisRef": action["actionRef"].split("#")[0],
                   "requestKey": key, "turnRef": turn_ref, "hostId": HOST_ID, "status": "PROPOSED", "createdAt": now(),
                   "newCaptureRefs": [], "resultAnalysisRef": None, "diagnostics": []}
            db.execute("INSERT INTO action_runs VALUES(?,?,?,?,?)", (run["actionRunId"], owner.key, run["actionRef"], run["originAnalysisRef"], canonical(run).decode()))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?)", (owner.key, "followup", key, identity, run["actionRunId"]))
            return self._write_run(db, owner, run, "RUNNING" if runnable else "AWAITING_INPUT", "FOLLOWUP_STARTED" if runnable else "CAPABILITY_OR_INPUT_UNAVAILABLE")

    def get_action_run(self, owner, ref):
        with closing(self._connect()) as db:
            run = self._run(db, owner, ref)
        return self._recover_foreign_host(owner, run)

    def _recover_foreign_host(self, owner, run):
        if run["status"] == "RUNNING" and run.get("hostId") != HOST_ID:
            return self.update_action_run(owner, run["actionRunId"], "PARTIAL", "HOST_RESTART_WITHOUT_RESULT", only_running=True)
        return run

    def list_action_runs(self, owner, origin=None):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT data FROM action_runs WHERE owner=? AND (? IS NULL OR origin=?) ORDER BY rowid", (owner.key, origin, origin)).fetchall()
        return [self._recover_foreign_host(owner, json.loads(row[0])) for row in rows]

    def update_action_run(self, owner, ref, status, code, only_running=False):
        with self._transaction() as db:
            run = self._run(db, owner, ref)
            if run["status"] == "COMPLETED" or (only_running and run["status"] != "RUNNING"):
                return run
            return self._write_run(db, owner, run, status, code)

    def record_action_capture(self, owner, ref, capture_ref, state):
        self.get_capture(owner, capture_ref)
        with self._transaction() as db:
            run = self._run(db, owner, ref)
            if run["status"] == "COMPLETED":
                return
            if capture_ref in run["newCaptureRefs"]:
                return
            if len(run["newCaptureRefs"]) >= 1000:
                self._write_run(db, owner, run, "PARTIAL" if run["status"] == "RUNNING" else run["status"], "ACTION_CAPTURE_LIMIT")
                return
            run["newCaptureRefs"].append(capture_ref)
            db.execute("INSERT OR IGNORE INTO pins VALUES(?,?)", (ref, capture_ref))
            failed = str(state).upper() not in ("SUCCESS", "SUCCEEDED")
            # A failed read may still be recovered within this turn. Keep the
            # failure as an event; only a published verified result can complete.
            self._write_run(db, owner, run, run["status"], "CAPTURE_FAILED" if failed else "CAPTURE_SAVED" if run["status"] == "RUNNING" else "CAPTURE_SAVED_AFTER_END")

    def recover_action_runs(self, owner):
        # Called when a new host reply runtime starts. Never synthesize success.
        for run in self.list_action_runs(owner):
            if run["status"] == "RUNNING":
                self.update_action_run(owner, run["actionRunId"], "PARTIAL", "HOST_RESTART_OR_PREVIOUS_TURN_INTERRUPTED", only_running=True)

    def get_presentation(self, owner, kind, ref):
        with closing(self._connect()) as db:
            row = db.execute("SELECT digest FROM presentations WHERE owner=? AND kind=? AND ref=?", (owner.key, kind, ref)).fetchone()
        return self._read(row["digest"]) if row else None

    def save_publication_proposal(self, owner, analysis_ref, request):
        ref = 'proposal_' + digest({'owner': owner.key, 'request': request})
        with self._transaction() as db:
            row = db.execute('SELECT state FROM analyses WHERE owner=? AND ref=?', (owner.key, analysis_ref)).fetchone()
            if not row or row['state'] != 'DRAFT':
                raise AnalysisError('NOT_DRAFT', '修复草案只能关联本会话尚未发布的分析。')
            blob = self._put(db, request)
            db.execute('INSERT OR IGNORE INTO publication_proposals VALUES(?,?,?,?)', (ref, owner.key, analysis_ref, blob))
        return ref

    def get_publication_proposal(self, owner, ref):
        with closing(self._connect()) as db:
            row = db.execute('SELECT digest FROM publication_proposals WHERE owner=? AND ref=?', (owner.key, ref)).fetchone()
        if not row: raise AnalysisError('NOT_FOUND', '当前会话没有该发布修复草案。')
        return self._read(row['digest'])

    def save_document_check(self, owner, analysis_ref, record):
        with self._transaction() as db:
            row = db.execute('SELECT state FROM analyses WHERE owner=? AND ref=?', (owner.key, analysis_ref)).fetchone()
            if not row or row['state'] != 'DRAFT':
                raise AnalysisError('NOT_DRAFT', '页面检查只记录当前会话的发布前草稿。')
            blob = self._put(db, record)
            db.execute('INSERT INTO document_checks VALUES(?,?,?,?,?,?) ON CONFLICT(owner,analysis,content,width) '
                       'DO UPDATE SET digest=excluded.digest,checked=excluded.checked',
                       (owner.key, analysis_ref, record['contentDigest'], record['viewport']['width'], blob, time.time()))
            # Bounded viewport history; obsolete digests cannot validate a new document.
            db.execute('DELETE FROM document_checks WHERE owner=? AND analysis=? AND rowid NOT IN '
                       '(SELECT rowid FROM document_checks WHERE owner=? AND analysis=? ORDER BY checked DESC LIMIT 8)',
                       (owner.key, analysis_ref, owner.key, analysis_ref))

    def document_checks(self, owner, analysis_ref, content_digest):
        self.get_analysis(owner, analysis_ref)
        with closing(self._connect()) as db:
            rows = db.execute('SELECT digest FROM document_checks WHERE owner=? AND analysis=? AND content=? ORDER BY width',
                              (owner.key, analysis_ref, content_digest)).fetchall()
        return [self._read(row['digest']) for row in rows]

    def register_presentation(self, owner, kind, ref, analysis_ref, payload):
        with self._transaction() as db:
            row = db.execute("SELECT state FROM analyses WHERE owner=? AND ref=?", (owner.key, analysis_ref)).fetchone()
            if not row or row["state"] != "PUBLISHED":
                raise AnalysisError("NOT_PUBLISHED", "派生展示必须属于本会话已发布的分析。")
            old = db.execute("SELECT digest FROM presentations WHERE owner=? AND kind=? AND ref=?", (owner.key, kind, ref)).fetchone()
            if old:
                previous = self._read(old["digest"])
                if digest(previous) != digest(payload):
                    raise AnalysisError("IMMUTABLE_DELIVERY", "已登记的展示不能覆盖。")
                return previous
            blob = self._put(db, payload)
            db.execute("INSERT INTO presentations VALUES(?,?,?,?,?)", (owner.key, kind, ref, analysis_ref, blob))
        return payload

    def list_presentations(self, owner, kind, analysis_ref):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT ref FROM presentations WHERE owner=? AND kind=? AND analysis=? ORDER BY rowid", (owner.key, kind, analysis_ref)).fetchall()
        return [r["ref"] for r in rows]

    def collect_expired(self, timestamp: float | None = None) -> dict:
        """Remove only old, unpinned extension objects; never published inputs."""
        cutoff = (time.time() if timestamp is None else timestamp) - self.limits.retention_seconds
        with self._transaction() as db:
            deleted = db.execute("DELETE FROM captures WHERE created<? AND ref NOT IN (SELECT capture FROM pins)", (cutoff,)).rowcount
            retained = {r[0] for r in db.execute("SELECT digest FROM captures UNION SELECT digest FROM analyses UNION SELECT digest FROM presentations UNION SELECT digest FROM publication_proposals UNION SELECT digest FROM document_checks")}
            removed = 0
            for path in self.blobs.iterdir():
                if path.is_symlink() or path.resolve().parent != self.blobs.resolve():
                    continue
                if path.is_file() and path.stat().st_mtime < cutoff and path.stem not in retained:
                    # Only our hashed JSON and mkstemp prefix, never unrelated files.
                    if (path.suffix == ".json" and len(path.stem) == 64 and all(c in "0123456789abcdef" for c in path.stem)) or path.name.startswith("pending-"):
                        path.unlink()
                        db.execute("DELETE FROM blobs WHERE digest=?", (path.stem,))
                        removed += 1
        return {"capturesRemoved": deleted, "blobsRemoved": removed}
