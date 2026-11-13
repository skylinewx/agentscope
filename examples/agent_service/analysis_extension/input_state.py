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
        raise AnalysisError('STALE_INPUT_SET', '分析属于另一组输入；按当前已采用来源重新构建，不能继续旧计算或发布。')
