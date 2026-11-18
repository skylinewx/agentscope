"""Record the model's current data needs; user coverage is reviewed separately."""
from copy import deepcopy
from .models import AnalysisError, digest


async def prepare(service, turn, declaration):
    from .conversation_context import selected_contexts
    from .turn_completion import TurnCompletion
    declaration = deepcopy(declaration)
    previous = turn.state.get('analysisRequirement', {})
    investigations = declaration.pop('investigations', previous.get('investigations', []))
    candidate = TurnCompletion(service, deepcopy(turn.state))
    candidate.task(**declaration)
    payload = {'userRequest': turn.state['request'],
               'selectedContextRequests': [{key: item.get(key) for key in ('contextRef', 'request')}
                                          for item in selected_contexts(candidate.state)]}
    quotes = [payload['userRequest'], *(item['request'] for item in payload['selectedContextRequests'])]
    if not isinstance(investigations, list) or len(investigations) > 20:
        raise AnalysisError('INVALID_ANALYSIS_REQUIREMENT', 'investigations 使用明确用户调查要求的列表。')
    checked = []
    for item in investigations:
        if (not isinstance(item, dict) or set(item) - {'requirement', 'userQuote', 'scope', 'requirementRef'}
                or any(not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 1200
                       for key in ('requirement', 'userQuote', 'scope'))
                or not any(item['userQuote'] in quote for quote in quotes)):
            raise AnalysisError('INVALID_ANALYSIS_REQUIREMENT', '调查要求须包含真实用户原话、事项和对象范围；原话是否支持调查由内容审查核对。')
        value = {key: item[key] for key in ('requirement', 'userQuote', 'scope')}
        checked.append({**value, 'requirementRef': 'requirement_' + digest([payload, value])[:24]})
    evidence = deepcopy(candidate.state['materials'])
    assessment = {'origin': 'MODEL_PLAN', 'materials': evidence, 'investigations': checked,
                  'basis': payload, 'explanation': 'AI 本次声明的数据需求；不代表查询失败或用户要求已全部覆盖。'}
    assessment['assessmentKey'] = digest(assessment)
    return declaration, assessment
