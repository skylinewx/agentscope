"""Example-app routes reusing workspace access and signed download capabilities."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response

from agentscope.app.deps import get_current_user_id
from .delivery import AnalysisDelivery
from .engine import RuleRegistry
from .models import AnalysisError, Limits, Owner, canonical
from .store import AnalysisStore
from .actions import resolve, followup_text
from .activation import load_config, resolve_activation

log = logging.getLogger(__name__)


def _error(exc):
    log.warning("Analysis presentation refused: %s", exc.code)
    status = 413 if exc.code in ("VIEW_TOO_LARGE", "REPORT_TOO_LARGE", "DOCUMENT_TOO_LARGE", "INPUT_TOO_LARGE", "STORE_FULL") else 404 if exc.code in ("NOT_FOUND", "NOT_REGISTERED") else 409
    raise HTTPException(status, detail=exc.as_dict()) from exc


async def _body(request, allowed):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 65536:
            raise HTTPException(413, "展示请求过大。")
    try:
        body = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "展示请求不是完整 JSON。") from exc
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise HTTPException(422, "展示请求包含不支持的字段。")
    return body


def create_analysis_router(config_path):
    router = APIRouter(prefix="/workspace/analyses", tags=["analysis"])
    path = Path(config_path).resolve()

    async def context(request, user_id, agent_id, session_id):
        app = request.app
        await app.state.resource_access_service.get_resource(user_id, "agent", agent_id)
        record = await app.state.storage.get_session(user_id, agent_id, session_id)
        if record is None or record.user_id != user_id or record.agent_id != agent_id:
            raise HTTPException(404, "当前用户无法访问该会话。")
        workspace = await app.state.workspace_service.resolve(user_id, agent_id, session_id)
        config = load_config(path)
        activation = await resolve_activation(app, user_id, agent_id, session_id, workspace, config)
        if not activation.enabled:
            raise AnalysisError("ANALYSIS_DISABLED", "宿主已关闭分析和报告能力。")
        store = AnalysisStore(path.parent / "analysis-data", Limits(**config.get("limits", {})))
        return AnalysisDelivery(store, Owner(user_id, agent_id, session_id), workspace.workdir,
                                RuleRegistry(config.get("rules", {})), activation.reports, activation.cards,
                                activation.actions)

    @router.get("/preview")
    async def preview(request: Request, agent_id: str, session_id: str, preview_ref: str,
                      format: str = 'html', user_id: str = Depends(get_current_user_id)):
        try:
            delivery = await context(request, user_id, agent_id, session_id)
            from .service import AnalysisService
            from .report import render
            async def authorized(): pass  # context() already checked user, session and current activation.
            service = AnalysisService(delivery.store, delivery.owner, authorized, delivery.rules,
                                      delivery=delivery, actions_enabled=delivery.actions_enabled)
            from .presentation import page
            result = await service.preview_content(preview_ref)
            if format == 'json':
                return Response(canonical({**result, 'document': page(result['document'])}), media_type='application/json', headers={'Cache-Control': 'no-store'})
            if format != 'html': raise HTTPException(422, 'format 仅支持 html/json。')
            free = result['document'].get('freeDocument')
            from .free_document import CSP
            return Response(free['html'] if free else render(result['document']), media_type='text/html', headers={
                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': ('sandbox allow-scripts; ' + CSP) if free else "sandbox; default-src 'none'; style-src 'unsafe-inline'"})
        except AnalysisError as exc:
            _error(exc)

    @router.post('/preview-check')
    async def preview_check(request: Request, agent_id: str, session_id: str, user_id: str = Depends(get_current_user_id)):
        body = await _body(request, ('previewRef', 'contentDigest', 'frameKey', 'measurements'))
        try:
            delivery = await context(request, user_id, agent_id, session_id)
            from .service import AnalysisService
            async def authorized(): pass
            service = AnalysisService(delivery.store, delivery.owner, authorized, delivery.rules,
                                      delivery=delivery, actions_enabled=delivery.actions_enabled)
            result = await service.record_preview_check(body.get('previewRef'), body.get('contentDigest'), body.get('frameKey'), body.get('measurements'))
            return Response(canonical(result), media_type='application/json', headers={'Cache-Control': 'no-store'})
        except AnalysisError as exc:
            _error(exc)

    @router.post("/resolve-action")
    async def resolve_action(request: Request, agent_id: str, session_id: str, user_id: str = Depends(get_current_user_id)):
        body = await _body(request, ("actionRef",))
        try:
            service = await context(request, user_id, agent_id, session_id)
            if not service.actions_enabled:
                raise AnalysisError("ACTIONS_DISABLED", "当前会话未启用追查能力。")
            _, action = resolve(service.store, service.owner, body.get("actionRef"))
            # Resolving does not start a run, create a message or query a source.
            return Response(canonical({"actionRef": action["actionRef"], "text": followup_text(action),
                "availability": action["availability"]}), media_type="application/json", headers={"Cache-Control": "no-store"})
        except AnalysisError as exc:
            _error(exc)

    @router.get("/view")
    async def view(request: Request, path: str = Query(..., max_length=256), agent_id: str = Query(..., max_length=256),
                   session_id: str = Query(..., max_length=256), offset: int = 0, limit: int = 12,
                   user_id: str = Depends(get_current_user_id)):
        try:
            service = await context(request, user_id, agent_id, session_id)
            return Response(canonical(service.view(path, offset, limit)), media_type="application/json", headers={"Cache-Control": "no-store"})
        except AnalysisError as exc:
            _error(exc)

    @router.post("/project-view")
    async def project_view(request: Request, agent_id: str, session_id: str, user_id: str = Depends(get_current_user_id)):
        body = await _body(request, ("analysisRef", "viewParameters", "offset", "limit"))
        try:
            service = await context(request, user_id, agent_id, session_id)
            result = service.project_view(body.get("analysisRef"), body.get("viewParameters"), body.get("offset", 0), body.get("limit", 12))
            return Response(canonical(result), media_type="application/json", headers={"Cache-Control": "no-store"})
        except AnalysisError as exc:
            _error(exc)

    @router.post("/restore")
    async def restore(request: Request, agent_id: str, session_id: str, user_id: str = Depends(get_current_user_id)):
        body = await _body(request, ("path",))
        try:
            service = await context(request, user_id, agent_id, session_id)
            return service.restore(body.get("path"))
        except AnalysisError as exc:
            _error(exc)

    @router.post("/render-report")
    async def render_report(request: Request, agent_id: str, session_id: str, user_id: str = Depends(get_current_user_id)):
        body = await _body(request, ("analysisRef", "requestKey", "viewParameters", "layout"))
        try:
            service = await context(request, user_id, agent_id, session_id)
            return service.render_report(body.get("analysisRef"), body.get("requestKey"), body.get("viewParameters"), body.get("layout", "standard"))
        except AnalysisError as exc:
            _error(exc)

    def token_resource(agent_id, session_id, analysis_ref, export_ref):
        return "analysis-report:" + canonical([agent_id, session_id, analysis_ref, export_ref]).decode("utf-8")

    @router.post("/report-token")
    async def report_token(request: Request, agent_id: str, session_id: str, analysis_ref: str, export_ref: str,
                           user_id: str = Depends(get_current_user_id)):
        try:
            service = await context(request, user_id, agent_id, session_id)
            service.report(analysis_ref, export_ref)
            token, expiry = request.app.state.workspace_service.sign_download_token(user_id, token_resource(agent_id, session_id, analysis_ref, export_ref))
            return {"token": token, "expiresAt": expiry}
        except AnalysisError as exc:
            _error(exc)

    @router.get("/report")
    async def report(request: Request, agent_id: str, session_id: str, analysis_ref: str, export_ref: str,
                     download: bool = False, token: str | None = None, x_user_id: str | None = Header(None)):
        try:
            if token:
                try:
                    user_id = request.app.state.workspace_service.verify_download_token(token, token_resource(agent_id, session_id, analysis_ref, export_ref))
                except ValueError as exc:
                    raise HTTPException(403, "报告下载凭证无效或已过期，请重新点击下载。") from exc
            else:
                user_id = await get_current_user_id(x_user_id)
            service = await context(request, user_id, agent_id, session_id)
            result = service.report(analysis_ref, export_ref)
            free = service._artifact(analysis_ref).get('freeDocument')
            return Response(result["html"], media_type="text/html", headers={"Cache-Control": "no-store",
                            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": ("sandbox allow-scripts; default-src 'none'; frame-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'" if free else "sandbox; default-src 'none'; style-src 'unsafe-inline'"),
                            "Content-Disposition": ('attachment' if download else 'inline') + f'; filename="{export_ref}.html"'})
        except AnalysisError as exc:
            _error(exc)

    return router
