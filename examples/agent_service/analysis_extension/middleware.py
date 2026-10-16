"""One owner context for capture, analysis tools and protocol guidance."""

import asyncio

from agentscope.middleware import MiddlewareBase
from agentscope.message import Msg

from .capture import ReceiptCapture
from .service import AnalysisService
from .tools import AnalysisBuild, AnalysisInspect, AnalysisPublish, AnalysisRenderReport, AnalysisFollowUp, QueryResultRead
from .capture import leased_mcp_origin
from .models import digest
from .actions import readonly_capability


class AnalysisMiddleware(MiddlewareBase):
    def __init__(self, store, owner, resource_ids, authorize, rules=None, number_formats=None, skill_versions=None, delivery=None, actions_enabled=False, finding_rules=None, sources=None):
        self.capture = ReceiptCapture(store, owner, frozenset(resource_ids), authorize, number_formats, sources)
        self.service = AnalysisService(store, owner, authorize, rules, skill_versions, delivery, actions_enabled, finding_rules, sources=sources)
        self.capture.action_ref_provider = lambda: self.service.actions.active_ref

    async def on_reply(self, agent, input_kwargs, next_handler):
        runtime = self.service.actions
        if self.service.actions_enabled:
            await self.service.authorize()
            runtime.store.recover_action_runs(runtime.owner)
            inputs = input_kwargs.get("inputs")
            messages = inputs if isinstance(inputs, list) else [inputs]
            users = [m for m in messages if isinstance(m, Msg) and m.role == "user"]
            runtime.user_text = "\n".join(b.text for m in users for b in m.content if getattr(b, "type", None) == "text")
            runtime.turn_ref = digest([m.id for m in users]) if users and all(b.type == "text" for m in users for b in m.content) else None

            async def capabilities():
                available = await agent.toolkit._get_available_tools([g.name for g in agent.toolkit.tool_groups])
                found = []
                for registration in available.values():
                    tool = registration.tool
                    origin = leased_mcp_origin(tool, self.capture.resource_ids)
                    capability = readonly_capability(tool, origin, self.capture.sources)
                    if capability:
                        found.append(capability)
                return found
            runtime.capability_provider = capabilities
        try:
            async for event in next_handler(**input_kwargs):
                reason = str(getattr(event, "finished_reason", ""))
                if reason == "interrupted":
                    runtime.finish("CANCELLED", "USER_OR_HOST_INTERRUPTED")
                elif reason == "error":
                    runtime.finish("FAILED", "HOST_REPLY_FAILED")
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            runtime.finish("CANCELLED", "USER_OR_HOST_INTERRUPTED")
            raise
        except Exception:
            runtime.finish("FAILED", "HOST_REPLY_FAILED")
            raise
        finally:
            runtime.finish()

    async def on_check_permission(self, agent, input_kwargs, next_handler):
        decision = await next_handler(**input_kwargs)
        if self.service.actions.active_ref and str(decision.behavior).lower().split(".")[-1] == "deny":
            self.service.actions.finish("FAILED", "SOURCE_PERMISSION_DENIED")
        return decision

    async def on_acting(self, agent, input_kwargs, next_handler):
        async for event in self.capture.run(agent, input_kwargs, next_handler):
            yield event

    async def list_tools(self):
        result = [QueryResultRead(self.service), AnalysisBuild(self.service), AnalysisInspect(self.service), AnalysisPublish(self.service)]
        if self.service.actions_enabled:
            result.append(AnalysisFollowUp(self.service))
        if self.service.delivery is not None and self.service.delivery.allow_reports:
            result.append(AnalysisRenderReport(self.service))
        return result

    async def on_system_prompt(self, agent, current_prompt):
        prompt = current_prompt + "\n\n统一结果宿主协议 v1：能力由宿主配置提供，Skill 指导何时使用，Skill 名称不决定工具装配。已授权且登记适配器的来源完整响应自动保存；预算 v2 原生接入，其他 MCP 的正常调用不受分析适配影响。大回执以 captured_query_result 返回摘要及 omitted 路径，QueryResultRead 可直接只读当前会话原回执，无需构建分析；读取分页不代替来源完整性。普通查询、筛选、排序和简单统计默认直接用文字或普通 Markdown 表格答复，不加载报告流程，不调用 AnalysisBuild/AnalysisPublish；排序用 SortRecords，计算用 Calculate。仅在用户要求分析、图表、结构化卡片或报告时使用相应 Skill 与构建发布能力。AnalysisInspect 提供会话索引，AnalysisBuild/AnalysisPublish 保存并展示模型明确选择的范围。发布只有 delivery.registered=true 才原样使用 delivery.markdown，不自行改链接。"
        if self.service.delivery is not None and self.service.delivery.allow_reports:
            prompt += "\n已启用独立报告能力：需要报告且已选择相关 Skill 时读取 report-generation，先确定用户所指的完整范围；综合多轮内容先按其 report-scope 指引汇集查询及分析主题，构建、发布覆盖这些主题的结果，再调用 AnalysisRenderReport。报告可以来自查询结果或分析结果，固定 analysisRef 只标识已发布的结构化结果；不要求为了出报告先开展分析。按已选报告 Skill 或用户要求编写 HTML，通过 document.bindings 绑定真实数值；预览发布后冻结页面，卡片与报告导出复用同一内容及登记入口。"
        if self.service.actions_enabled:
            prompt += "\n已启用分析追问能力：带行动引用的真实用户追问已选择 data-analysis 时先读其 actions-and-follow-up，再调用 AnalysisFollowUp；分析方法和行动选择遵循该 Skill。"
        return prompt
