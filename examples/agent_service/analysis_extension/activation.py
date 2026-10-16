"""Host capability policy, independent of selected Skill names."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from .models import AnalysisError

CONFIG_FIELDS = frozenset({"schemaVersion", "activation", "capabilities", "sources", "numberFormats", "rules", "findingRules", "limits"})


def validate_config(config):
    if not isinstance(config, dict) or config.get("schemaVersion") != 1:
        raise AnalysisError("UNSUPPORTED_VERSION", "分析扩展配置版本不受支持。")
    if set(config) - CONFIG_FIELDS:
        raise AnalysisError("INVALID_CONFIG", "分析配置包含不支持的字段。")
    if config.get("activation", "auto") not in ("auto", "disabled"):
        raise AnalysisError("INVALID_MODE", "分析装配方式必须是 auto 或 disabled。")
    capabilities = config.get("capabilities", {})
    if (not isinstance(capabilities, dict) or set(capabilities) - {"analysis", "reports"}
            or any(type(value) is not bool for value in capabilities.values())):
        raise AnalysisError("INVALID_CONFIG", "capabilities 仅接受 analysis、reports 布尔开关。")
    return config


def load_config(path):
    path = Path(path)
    return validate_config(json.loads(path.read_text("utf-8")) if path.exists() else {"schemaVersion": 1})


@dataclass(frozen=True)
class Activation:
    skill_versions: dict = field(default_factory=dict)
    analysis_enabled: bool = True
    reports_enabled: bool = True

    @property
    def enabled(self):
        return self.analysis_enabled or self.reports_enabled

    @property
    def actions(self):
        return self.analysis_enabled

    @property
    def cards(self):
        return self.enabled

    @property
    def reports(self):
        return self.reports_enabled


async def selected_skill_versions(app, user_id, agent_id, session_id, workspace):
    resources = getattr(app.state, "resource_service", None)
    if resources is None:
        return {}
    selected = []
    for selection, record in await resources.effective(user_id, agent_id, session_id):
        if selection.kind != "skill" or not selection.enabled:
            continue
        if not record or record.user_id != user_id:
            continue
        selected.append((selection, record))
    actual = {}
    for selection, record in selected:
        if selection.resource_id in actual:
            raise AnalysisError("SKILL_VERSION_MISMATCH", "本会话选择了重复的 Skill 资源，不能确认唯一版本。")
        try:
            skill = await resources._session_skill(
                user_id, workspace, agent_id, session_id, selection, install=True
            )
        except (ValueError, OSError, RuntimeError) as exc:
            raise AnalysisError("SKILL_VERSION_MISMATCH", "当前会话所选 Skill 安装包不可用，请重新发布并加载该包。") from exc
        value = await resources._skill_hash(workspace, skill.dir)
        if not selection.binding.artifact_id or value != selection.binding.artifact_id:
            raise AnalysisError("SKILL_VERSION_MISMATCH", "当前安装的所选 Skill 与已登记安装包不一致，请重新发布并加载该包。")
        actual[selection.resource_id] = value
    return actual


async def resolve_activation(app, user_id, agent_id, session_id, workspace, config):
    """Runtime tools and HTTP routes share host policy and package checks."""
    validate_config(config)
    if config.get("activation", "auto") == "disabled":
        return Activation({}, False, False)
    capabilities = config.get("capabilities", {})
    analysis, reports = capabilities.get("analysis", True), capabilities.get("reports", True)
    if not (analysis or reports):
        return Activation({}, False, False)
    actual = await selected_skill_versions(app, user_id, agent_id, session_id, workspace)
    return Activation(actual, analysis, reports)


async def selected_resource_ids(app, user_id, agent_id, session_id, workspace):
    """Read existing MCP bindings; never discover or connect a server."""
    resources = getattr(app.state, 'resource_service', None)
    if resources is None:
        return []
    selected = set()
    for selection, record in await resources.effective(user_id, agent_id, session_id):
        if selection.kind == "mcp" and selection.enabled and record and record.user_id == user_id:
            selected.add(selection.resource_id)
    return sorted(selected)
