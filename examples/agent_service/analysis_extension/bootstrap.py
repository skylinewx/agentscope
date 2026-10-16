"""Skill-driven example-service integration; re-read policy per runtime."""
from __future__ import annotations

from pathlib import Path

from .activation import load_config, resolve_activation, selected_resource_ids
from .engine import RuleRegistry
from .delivery import AnalysisDelivery
from .middleware import AnalysisMiddleware
from .models import AnalysisError, Limits, Owner
from .store import AnalysisStore
from .adapters import default_sources


async def create_analysis_middlewares(app, user_id, agent_id, session_id, workspace, config_path):
    path = Path(config_path)
    config = load_config(path)
    owner = Owner(user_id, agent_id, session_id)

    async def authorize():
        await app.state.resource_access_service.get_resource(user_id, "agent", agent_id)
        session = await app.state.storage.get_session(user_id, agent_id, session_id)
        if session is None or session.agent_id != agent_id or session.user_id != user_id:
            raise AnalysisError("ACCESS_DENIED", "当前用户无法访问该智能体会话。")

    await authorize()
    activation = await resolve_activation(app, user_id, agent_id, session_id, workspace, config)
    if not activation.enabled:
        return []
    resource_ids = await selected_resource_ids(app, user_id, agent_id, session_id, workspace)
    # Fixed service-private sibling of config; never a model-selected path.
    root = path.resolve().parent / "analysis-data"
    workspace_path = Path(workspace.workdir).resolve()
    if root.is_relative_to(workspace_path):
        raise AnalysisError("UNSAFE_STORE", "分析存储不能位于模型工作区内。")
    store = AnalysisStore(root, Limits(**config.get("limits", {})))
    store.collect_expired()
    rules = RuleRegistry(config.get("rules", {}))
    sources = default_sources(config.get('sources'))
    delivery = AnalysisDelivery(store, owner, workspace_path, rules,
                                activation.reports, activation.cards, activation.actions)
    return [AnalysisMiddleware(store, owner, resource_ids, authorize,
            rules, config.get("numberFormats", {}), activation.skill_versions,
            delivery=delivery,
            actions_enabled=activation.actions, finding_rules=config.get("findingRules", {}), sources=sources)]
