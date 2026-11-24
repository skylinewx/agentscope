"""Keep asynchronous work bound to the explicitly adopted input set."""
from .models import AnalysisError


def snapshot(service):
    turn = service.turn
    state = turn.state if turn else {}
    from .models import digest
    return turn, state.get('turnKey'), state.get('inputSetVersion'), digest([state.get('objectives', []), state.get('scopeEdits', [])])


def require_unchanged(service, expected):
    if snapshot(service) != expected:
        raise AnalysisError('TASK_INPUTS_CHANGED', '执行期间任务或采用范围已变化；旧执行保留记录，不覆盖当前输入组。')


def _current_report(state, ref):
    if ref == state.get('adoptedReportRef') or ref in state.get('buildRefs', ()):
        return True
    if state.get('mode') == 'REPORT':
        from .conversation_context import selected_contexts
        reports = {item['analysisRef'] for item in selected_contexts(state) if item.get('analysisRef')}
        return reports == {ref}
    return False


def _direct_query_error(state):
    if state.get('queryCaptureRefs') or state.get('queryResultRefs'):
        return AnalysisError('STALE_INPUT_SET', '这个报告属于其他输入。本轮是直接查询，请查看本轮查询回执并按其结果回答。',
                             {'currentMode': 'DIRECT'})
    return AnalysisError('STALE_INPUT_SET', '这个报告属于其他输入。请按本轮目标完成直接查询。')


def validate_analysis(service, artifact, *, repairing=False):
    turn = service.turn
    ref = artifact['analysisRef']
    if (repairing and turn and turn.state.get('mode') == 'DIRECT'
            and (turn.state.get('queryCaptureRefs') or turn.state.get('queryResultRefs'))
            and not _current_report(turn.state, ref)):
        raise _direct_query_error(turn.state)
    if (turn and turn.state.get('adoptedInputs')
            and artifact.get('plan', {}).get('inputSetVersion') != turn.state.get('inputSetVersion')):
        plan = artifact.get('plan', {})
        if plan.get('selections'):
            # Re-indexing a complete report can change adoption metadata, but
            # not its inputs. Prove exact source/row/column/grain/role equality;
            # a matching label, value or metric code is never sufficient.
            from .source_views import selection_identity
            current = [item['selection'] for item in turn.state['adoptedInputs']]
            if selection_identity(turn, plan['selections']) == selection_identity(turn, current):
                return
        if not _current_report(turn.state, ref):
            if turn.state.get('mode') == 'DIRECT':
                raise _direct_query_error(turn.state)
            raise AnalysisError('STALE_INPUT_SET', '当前采用的输入与报告完整来源不同。请使用本轮来源继续分析。')
        raise AnalysisError('STALE_INPUT_SET', '当前采用的输入与报告完整来源不同。处理这份报告可按下面的参数恢复其完整来源。',
            {'analysisRef': ref, 'restoreOriginalReportInputs': {
                'tool': 'AnalysisTask', 'arguments': {'action': 'declare',
                    'mode': turn.state.get('mode') or 'ANALYSIS',
                    'goal': turn.state.get('goal') or turn.state.get('request'),
                    'analysisRef': ref},
                'meaning': '仅在用户仍要处理原报告时选择；不会重查或重算，也不自动改变当前采用范围。'}})
