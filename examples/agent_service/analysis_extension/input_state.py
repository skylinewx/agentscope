"""Keep asynchronous work bound to the explicitly adopted input set."""
from .models import AnalysisError


def snapshot(service):
    turn = service.turn
    state = turn.state if turn else {}
    return turn, state.get('turnKey'), state.get('inputSetVersion')


def require_unchanged(service, expected):
    if snapshot(service) != expected:
        raise AnalysisError('TASK_INPUTS_CHANGED', '执行期间任务或采用范围已变化；旧执行保留记录，不覆盖当前输入组。')


def validate_analysis(service, artifact):
    turn = service.turn
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
        raise AnalysisError('STALE_INPUT_SET', '当前采用的输入与报告完整来源不同。若仍处理原报告，按 analysisRef 重新采用其完整输入；只有确实改变范围才需要重新构建。',
            {'analysisRef': artifact['analysisRef'], 'restoreOriginalReportInputs': {
                'tool': 'AnalysisTask', 'arguments': {'action': 'declare',
                    'mode': turn.state.get('mode') or 'ANALYSIS',
                    'goal': turn.state.get('goal') or turn.state.get('request'),
                    'analysisRef': artifact['analysisRef']},
                'meaning': '仅在用户仍要处理原报告时选择；不会重查或重算，也不自动改变当前采用范围。'}})
