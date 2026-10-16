"""Registered projections are separate from immutable analysis revisions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .engine import RuleRegistry
from .models import AnalysisError, ENGINE_VERSION, canonical, digest
from .presentation import MANIFEST_MAX_BYTES, RENDERER_VERSION, page, parameters, project, published
from .report import REPORT_MAX_BYTES, REPORT_TEMPLATE_VERSION, render

REFERENCE = re.compile(r"(ana_[0-9a-f]{32}):([1-9][0-9]{0,8})")
MANIFEST = re.compile(r"\./analysis/(ana_[0-9a-f]{32})/revisions/([1-9][0-9]{0,8})/analysis\.json")


def manifest_path(ref):
    match = REFERENCE.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise AnalysisError("INVALID_REFERENCE", "分析引用格式不正确。")
    return f"./analysis/{match[1]}/revisions/{match[2]}/analysis.json"


def reference_from_path(path):
    # Canonical ASCII path only; no decoding, normalization or Windows aliases.
    match = MANIFEST.fullmatch(path) if isinstance(path, str) else None
    if not match:
        raise AnalysisError("INVALID_PATH", "请使用工具返回的完整分析链接。")
    return f"{match[1]}:{match[2]}"


class AnalysisDelivery:
    def __init__(self, store, owner, workspace, rules=None, allow_reports=True, cards_enabled=True, actions_enabled=False):
        self.store, self.owner = store, owner
        self.root = Path(workspace).resolve()
        if store.root.is_relative_to(self.root):
            raise AnalysisError("UNSAFE_STORE", "分析私有存储不能位于展示工作区内。")
        self.rules = rules or RuleRegistry()
        self.allow_reports, self.cards_enabled = allow_reports, cards_enabled
        self.actions_enabled = actions_enabled

    def _path(self, relative):
        parts = relative.removeprefix("./").split("/")
        if not parts or parts[0] != "analysis" or any(p in ("", ".", "..") or ":" in p or "\\" in p for p in parts):
            raise AnalysisError("INVALID_PATH", "展示路径不属于分析工作区。")
        candidate = self.root
        for part in parts:
            candidate = candidate / part
            # No path component may redirect. Re-resolving a concurrently-created
            # deep Windows path can produce transient false escapes; check each
            # reparse point and the already-absolute, constrained spelling instead.
            if candidate.is_symlink() or candidate.is_junction() or not candidate.absolute().is_relative_to(self.root):
                raise AnalysisError("UNSAFE_PATH", "展示路径不能指向工作区之外。")
        return candidate

    def _check(self, relative, content, maximum):
        path = self._path(relative)
        try:
            with path.open("rb") as stream:
                actual = stream.read(maximum + 1)
        except FileNotFoundError as exc:
            raise AnalysisError("MISSING_PROJECTION", "展示文件缺失，请恢复已登记的原版本。") from exc
        if len(actual) > maximum or hashlib.sha256(actual).digest() != hashlib.sha256(content).digest():
            raise AnalysisError("PROJECTION_CHANGED", "展示文件与已登记版本不一致，请恢复原版本后重试。")
        return actual

    def _write(self, relative, content, restore=False):
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path(relative)
        if path.exists() and not restore:
            self._check(relative, content, len(content))
            return
        fd, temporary = tempfile.mkstemp(prefix="pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(relative)
            if restore:
                os.replace(temporary, path)
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    self._check(relative, content, len(content))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _artifact(self, ref):
        manifest_path(ref)
        artifact = self.store.get_analysis(self.owner, ref)
        published(artifact)
        return artifact

    def ensure(self, ref):
        artifact = self._artifact(ref)
        saved = self.store.get_presentation(self.owner, "delivery", ref)
        if not saved:
            if artifact["ruleVersion"] != self.rules.version:
                raise AnalysisError("RULE_VERSION_UNAVAILABLE", "发布时的规则版本不可用，请重新构建分析。")
            document = project(artifact, self.rules)
            manifest = {"schemaVersion": 1, "analysisRef": ref, "dataDigest": digest(artifact),
                        "rendererVersion": RENDERER_VERSION, "snapshotDigest": digest(document)}
            saved = {"manifest": manifest, "path": manifest_path(ref), "document": document, "rules": self.rules.rules}
            # A failed file write must not be reported as a successful card delivery.
            self._write(saved["path"], canonical(manifest))
            saved = self.store.register_presentation(self.owner, "delivery", ref, ref, saved)
        self._validate(saved, artifact)
        return {"type": "workspace_analysis", "registered": True, "analysisRef": ref, "path": saved["path"],
                "markdown": f"[查看结果]({saved['path']})", "rendererVersion": RENDERER_VERSION,
                "processSummary": {"kind": "analysis", "id": ref.split(':')[0]}}

    def _validate(self, saved, artifact, check_file=True):
        if any(value.get("schemaVersion") != 1 for value in (saved["manifest"], saved["document"])):
            raise AnalysisError("UNSUPPORTED_VERSION", "仅支持当前展示登记协议。")
        renderer = saved["manifest"].get("rendererVersion")
        if not isinstance(renderer, str) or not renderer or renderer != saved["document"].get("rendererVersion"):
            raise AnalysisError("INVALID_PRESENTATION", "展示登记的渲染版本不一致。")
        document = saved["document"]
        if document.get("engineVersion") != ENGINE_VERSION:
            raise AnalysisError("UNSUPPORTED_VERSION", "仅支持当前计算引擎的展示结果。")
        if (type(document.get("technicalDetails")) is not bool or type(document.get("chartsRequested")) is not bool or not isinstance(document.get("findings"), list)
                or not isinstance(document.get("actions"), list) or not isinstance(document.get("findingsScopeLabel"), str)):
            raise AnalysisError("INVALID_PRESENTATION", "分析展示缺少当前协议要求的字段。")
        if saved["manifest"]["dataDigest"] != digest(artifact) or saved["manifest"]["snapshotDigest"] != digest(saved["document"]):
            raise AnalysisError("DIGEST_MISMATCH", "展示登记与已发布分析不一致。")
        if check_file:
            self._check(saved["path"], canonical(saved["manifest"]), MANIFEST_MAX_BYTES)

    def _saved(self, ref, check_file=True):
        artifact = self._artifact(ref)
        saved = self.store.get_presentation(self.owner, "delivery", ref)
        if not saved:
            raise AnalysisError("NOT_REGISTERED", "本会话没有已登记的分析展示。")
        self._validate(saved, artifact, check_file)
        return artifact, saved

    def view(self, path, offset=0, limit=12):
        ref = reference_from_path(path)
        result = page(self._project(ref, parameters()), offset, limit)
        result["capabilities"] = {"cards": self.cards_enabled, "generateReports": self.allow_reports, "followUp": self.actions_enabled}
        result["actionRuns"] = [{k: run[k] for k in ("actionRunId", "actionRef", "status", "resultAnalysisRef", "updatedAt")}
                                | {"resultNote": {
                                    "FORMULA_CHECKED_MATCH": "本次公式核对完成，与来源值一致。",
                                    "FORMULA_CHECKED_WITH_DIFFERENCE": "本次公式核对完成，存在差额；核对完成不代表公式一致。",
                                    "FORMULA_EVIDENCE_REQUIRED": "公式或依赖核对仍有缺口。",
                                    "ACTION_FORMULA_CHANGED": "来源公式已变化，需要新建关联分析。",
                                }.get(run["diagnostics"][-1]["code"], "")}
                                for run in self.store.list_action_runs(self.owner, ref)[-100:]]
        result["reports"] = [self._export(ref, key)["delivery"]
                             for key in self.store.list_presentations(self.owner, "export", ref)[-100:]]
        from .presentation import VIEW_MAX_BYTES
        if len(canonical(result)) > VIEW_MAX_BYTES:
            raise AnalysisError("VIEW_TOO_LARGE", "视图超过响应上限，请缩小展示范围。")
        return result

    def restore(self, path):
        ref = reference_from_path(path)
        _, saved = self._saved(ref, check_file=False)
        self._write(path, canonical(saved["manifest"]), restore=True)
        return self.view(path)

    def _project(self, ref, params):
        artifact, saved = self._saved(ref)
        params = parameters(params)
        # Re-render the published current-schema artifact, never parse or
        # migrate a layout snapshot. Frozen facts, claims, rules and exports
        # remain untouched; reading a card does not write a new publication.
        return project(artifact, RuleRegistry(saved["rules"]), params)

    def project_view(self, ref, params, offset=0, limit=12):
        return page(self._project(ref, params), offset, limit)

    def render_report(self, analysisRef, requestKey, viewParameters=None, layout="standard"):
        if not self.allow_reports:
            raise AnalysisError("REPORTS_DISABLED", "当前会话未启用报告能力。")
        if not isinstance(requestKey, str) or not requestKey or len(requestKey) > 256:
            raise AnalysisError("INVALID_REQUEST_KEY", "报告请求需要有界的幂等标识。")
        params = parameters(viewParameters)
        artifact, saved = self._saved(analysisRef)
        request = {"analysisRef": analysisRef, "viewParameters": params, "layout": layout,
                   "rendererVersion": RENDERER_VERSION, "reportTemplateVersion": REPORT_TEMPLATE_VERSION}
        request_ref = digest(requestKey)
        previous = self.store.get_presentation(self.owner, "report-request", request_ref)
        if previous:
            if previous["request"] != request:
                raise AnalysisError("REQUEST_CONFLICT", "同一个报告请求标识不能用于不同范围或布局。")
            return self.report(analysisRef, previous["exportRef"])["delivery"]
        export_ref = "export_" + digest(request)
        old = self.store.get_presentation(self.owner, "export", export_ref)
        if old:
            self.report(analysisRef, export_ref)
            self.store.register_presentation(self.owner, "report-request", request_ref, analysisRef, {"request": request, "exportRef": export_ref})
            return old["delivery"]
        document = self._project(analysisRef, params)
        content = render(document, layout)
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = saved["path"].removesuffix("analysis.json") + f"reports/{export_ref}.html"
        self._write(path, content.encode("utf-8"))
        result = {"analysisRef": analysisRef, "exportRef": export_ref, "path": path, "contentDigest": content_digest,
                  "scopeLabel": document["scopeLabel"], "isFiltered": document["isFiltered"], "viewParameters": params,
                  "rendererVersion": RENDERER_VERSION, "layout": layout, "sizeBytes": len(content.encode("utf-8")), "registered": True}
        result["processSummary"] = {"kind": "report", "id": export_ref,
            "topicCount": 0 if document["isFiltered"] else len(artifact["scope"].get("reportTopics", []))}
        result["markdown"] = f"[查看报告]({saved['path']})"
        payload = {"delivery": result, "html": content}
        self.store.register_presentation(self.owner, "export", export_ref, analysisRef, payload)
        self.store.register_presentation(self.owner, "report-request", request_ref, analysisRef, {"request": request, "exportRef": export_ref})
        self.report(analysisRef, export_ref)
        return result

    def _export(self, ref, export_ref):
        result = self.store.get_presentation(self.owner, "export", export_ref)
        if not result or result["delivery"]["analysisRef"] != ref:
            raise AnalysisError("NOT_REGISTERED", "本会话没有对应版本的已登记报告。")
        if not isinstance(result["delivery"].get("rendererVersion"), str) or not result["delivery"]["rendererVersion"]:
            raise AnalysisError("INVALID_PRESENTATION", "报告缺少生成版本。")
        if result["delivery"].get("layout") not in ("standard", "table"):
            raise AnalysisError("INVALID_PRESENTATION", "报告缺少有效的布局字段。")
        return result

    def report(self, ref, export_ref):
        self._artifact(ref)
        result = self._export(ref, export_ref)
        content = result["html"].encode("utf-8")
        if hashlib.sha256(content).hexdigest() != result["delivery"]["contentDigest"]:
            raise AnalysisError("DIGEST_MISMATCH", "报告内容与登记不符。")
        self._check(result["delivery"]["path"], content, REPORT_MAX_BYTES)
        return result
