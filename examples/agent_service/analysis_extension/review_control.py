"""同一问题的有限补做和审查复用；正常取数不计为补做。"""
import asyncio
from copy import deepcopy
from contextlib import contextmanager
import logging
import re
import time
from weakref import WeakKeyDictionary

from .models import AnalysisError, digest


_INFLIGHT = WeakKeyDictionary()


def turn_key(turn):
    return turn.state.get('turnKey') or digest([turn.state.get('request'), turn.state.get('goal')])


def progress(turn, update=None):
    return turn.service.store.turn_progress(turn.service.owner, turn_key(turn), update)


@contextmanager
def measure_stage(turn, stage):
    """登记宿主内部阶段；与工具总耗时有包含关系，不能简单相加。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        if turn is not None:
            from .timing import timing_event, append_timing
            event = timing_event(turn, stage, elapsed, includedIn='publication')
            def record(state):
                append_timing(state, event)
                stats = state.setdefault('stages', {}).setdefault(stage, {'calls': 0, 'elapsedMs': 0})
                stats['calls'] += 1
                stats['elapsedMs'] = round(stats['elapsedMs'] + elapsed, 2)
            progress(turn, record)
            logging.getLogger(__name__).info('分析阶段 提问=%s 阶段=%s 耗时毫秒=%.2f', turn_key(turn), stage, elapsed)


def request_closeout(turn, reason):
    """明确的容量或重试耗尽结果立即终止补做，不等模型再发一次请求。"""
    return progress(turn, lambda state: state.setdefault('closeoutReason', reason))


def closeout_reason(turn):
    limits = turn.service.store.limits
    def check(state):
        started = state.get('repairStartedAt')
        if started and time.time() - started >= limits.repair_budget_seconds:
            state.setdefault('closeoutReason', 'REPAIR_TIME_LIMIT')
    return progress(turn, check).get('closeoutReason')


def reserve_review(turn):
    limits = turn.service.store.limits
    def reserve(state):
        review = state.setdefault('review', {'attempts': 0, 'elapsedMs': 0, 'unavailable': 0})
        if (review['attempts'] >= limits.max_review_attempts
                or review['elapsedMs'] >= limits.review_budget_seconds * 1000):
            state.setdefault('closeoutReason', 'REVIEW_LIMIT')
        if not state.get('closeoutReason'):
            review['attempts'] += 1
    state = progress(turn, reserve)
    if state.get('closeoutReason'):
        raise AnalysisError('ANALYSIS_RETRY_LIMIT', '本轮自动补做已到限额，将交付已有有效结果。',
                            {'requiresBuild': False, 'nextAction': 'HOST_CLOSEOUT'})


def finish_review(turn, elapsed_ms, verdict=None, unavailable=False, attempt=None):
    def finish(state):
        if attempt is not None:
            attempts = state.setdefault('reviewAttempts', [])
            if any(item['reviewId'] == attempt['reviewId'] for item in attempts):
                return
            attempts.append(deepcopy(attempt))
            del attempts[:-32]
            totals = state.setdefault('reviewTotals', {'attempts': 0, 'elapsedMs': 0, 'inputChars': 0,
                'completedPartCount': 0, 'reusedPartCount': 0, 'chargedAttempts': 0, 'chargedElapsedMs': 0})
            totals['attempts'] += 1
            totals['chargedAttempts'] += int(attempt['charged'])
            totals['chargedElapsedMs'] = round(totals['chargedElapsedMs'] + elapsed_ms, 2)
            for field in ('elapsedMs', 'inputChars', 'completedPartCount', 'reusedPartCount'):
                totals[field] = round(totals[field] + attempt[field], 2)
        review = state.setdefault('review', {'attempts': 0, 'elapsedMs': 0, 'unavailable': 0})
        review['elapsedMs'] = round(review['elapsedMs'] + elapsed_ms, 2)
        if verdict is not None:
            state['lastReviewBlocked'] = bool(verdict['issues'])
            if verdict['issues'] and review['attempts'] >= turn.service.store.limits.max_review_attempts:
                state.setdefault('closeoutReason', 'REVIEW_LIMIT')
        if unavailable:
            review['unavailable'] += 1
            # 一次原材料重试后仍不可用，宿主直接收尾，模型不重写页面。
            if review['unavailable'] >= 2:
                state.setdefault('closeoutReason', 'REVIEW_UNAVAILABLE')
    return progress(turn, finish)


def sync_review_metrics(turn, persisted=None):
    """后台与恢复后的会话读取同一份耗时，缓存命中不算新的模型调用。"""
    persisted = persisted if persisted is not None else progress(turn)
    for source, target in (('reviewAttempts', 'analysisReviewAttempts'), ('reviewTotals', 'analysisReviewTotals')):
        if source in persisted:
            turn.state[target] = deepcopy(persisted[source])


def locate_review_statements(statement, material):
    """仅接收原文中的完整摘录；解释夹带的引文分别核对，不做语义近似匹配。"""
    fields = ('documentText', 'documentSourceText', 'candidateAnswer')
    normalized = {field: re.sub(r'\s+', ' ', material.get(field, '')).strip() for field in fields}

    def locations(candidate):
        quote = re.sub(r'\s+', ' ', candidate).strip()
        return [field for field in fields if quote and quote in normalized[field]]

    exact = locations(statement)
    if exact:
        return [{'statement': statement, 'locations': exact}]
    # 模型有时在 statement 中加入位置说明并引用多个片段。只提取明确的
    # 配对引号，随后仍要求该片段逐字出现在本次材料中；不从意见中猜原句。
    candidates = re.findall(r'「([^「」]+)」|“([^“”]+)”|『([^『』]+)』|"([^"\n]+)"', statement)
    found = []
    seen = set()
    for quoted in candidates:
        candidate = next(text for text in quoted if text)
        matching = locations(candidate)
        if matching and candidate not in seen:
            found.append({'statement': candidate, 'locations': matching})
            seen.add(candidate)
    return found


def record_review_issue(turn, item, material_key, material):
    """按实际原文与证据标识问题；保留各次严重度，不能锁死第一次判断。"""
    statement = item.get('statement', '')
    matches = locate_review_statements(statement, material)
    valid_statement = matches[0]['statement'] if matches else ''
    related = [match['statement'] for match in matches[1:]]
    locations = list(dict.fromkeys(field for match in matches for field in match['locations']))
    known = {fact['factId'] for fact in material.get('facts', [])}
    known.update(fact['factId'] for fact in material.get('supportingEvidence', {}).get('facts', []))
    known.update(value['derivedId'] for value in material.get('derived', []))
    refs = sorted(set(item.get('evidenceRefs', [])) & known)
    issue_id = 'review_issue_' + digest([item['code'], [valid_statement, *related] if matches else item['message'], refs])[:24]
    def record(state):
        ledger = state.setdefault('reviewIssueHistory', {})
        record = ledger.setdefault(issue_id, {'issueId': issue_id, 'code': item['code'],
                                              'statement': valid_statement, 'evidenceRefs': refs, 'observations': []})
        if related:
            record['relatedStatements'] = related
        if statement != valid_statement:
            record['reportedStatement'] = statement
        observation = {'materialKey': material_key, 'severity': item['severity'], 'message': item['message']}
        if observation not in record['observations']:
            record['observations'].append(observation)
            del record['observations'][:-4]
        while len(ledger) > 32:
            ledger.pop(next(iter(ledger)))
    progress(turn, record)
    # 修正调用直接取得已验证的原句和证据引用；无法定位不被误判为通过。
    result = {key: item[key] for key in ('code', 'message')}
    result.update(issueId=issue_id, statement=valid_statement, evidenceRefs=refs,
                  statementLocations=locations,
                  locationStatus='LOCATED' if valid_statement else 'UNLOCATED')
    if related:
        result['relatedStatements'] = related
    if statement != valid_statement:
        result['reportedStatement'] = statement
    return result


def record_repair(turn, call_id, code):
    """参数修正、内容修正和暂时故障分别计数，共享持续时间及总次数边界。"""
    if code == 'ANALYSIS_RETRY_LIMIT':
        return
    def record(state):
        calls = state.setdefault('repairCalls', {})
        if call_id in calls:
            return
        calls[call_id] = code
        state.setdefault('repairStartedAt', time.time())
        limits = turn.service.store.limits
        # 只有内容审查确实要求修订才消耗内容补做次数。绑定、范围等确定性
        # 检查也可能返回 UNSUPPORTED_CLAIM，不能仅凭同名错误当成模型审查。
        semantic = {'PUBLICATION_REVISE', 'DOCUMENT_SEMANTIC_ERRORS'}
        transient = {'REVIEW_UNAVAILABLE', 'DELIVERY_IO', 'STORAGE_TEMPORARILY_UNAVAILABLE',
                     'QUERY_TIMEOUT', 'SOURCE_UNAVAILABLE'}
        category = 'semantic' if code in semantic else 'transient' if code in transient else 'parameter'
        counts = state.setdefault('repairCounts', {})
        counts[category] = counts.get(category, 0) + 1
        category_limit = {'semantic': limits.max_review_attempts, 'transient': 2,
                          'parameter': limits.max_analysis_repairs}[category]
        if counts[category] >= category_limit:
            state.setdefault('closeoutReason', 'REPAIR_LIMIT')
        if len(calls) >= limits.max_analysis_repairs + limits.max_review_attempts:
            state.setdefault('closeoutReason', 'REPAIR_LIMIT')
    progress(turn, record)


async def shared_review(turn, material_key, execute):
    """同进程同会话的相同在途审查只执行一次，完成结果由原持久化缓存保存。"""
    jobs = _INFLIGHT.setdefault(asyncio.get_running_loop(), {})
    key = (str(turn.service.store.root.resolve()), turn.service.owner.key, material_key)
    job = jobs.get(key)
    if job is None:
        job = {'task': asyncio.create_task(execute()), 'waiters': 0}
        jobs[key] = job
    job['waiters'] += 1
    try:
        return deepcopy(await asyncio.shield(job['task']))
    finally:
        job['waiters'] -= 1
        if not job['waiters']:
            if not job['task'].done():
                job['task'].cancel()
            if jobs.get(key) is job:
                jobs.pop(key, None)


async def call_review_model(turn, execute, *, phase, input_chars, round_started):
    """两类审查共用剩余额度；逐次记录模型请求，暂时故障仅在额度内重试。"""
    from agentscope.model._diagnostics import error_diagnostics
    spent = progress(turn).get('review', {}).get('elapsedMs', 0) / 1000
    # 当前轮已经完成的调用尚未结算，调用方传入的轮次起点用于准确扣减。
    for index in range(2):
        remaining = turn.service.store.limits.review_budget_seconds - spent - (time.perf_counter() - round_started)
        if remaining <= 0:
            request_closeout(turn, 'REVIEW_LIMIT')
            raise AnalysisError('ANALYSIS_RETRY_LIMIT', '本轮核对额度已用完，将整理已取得的结果。')
        call_started = time.perf_counter()
        error_code = None
        try:
            return await asyncio.wait_for(execute(), timeout=min(90, remaining))
        except asyncio.CancelledError:
            error_code = 'CANCELLED'
            raise
        except Exception as error:
            error_code = getattr(error, 'code', type(error).__name__)
            diagnostic = error_diagnostics(error) or {}
            transient = isinstance(error, (TimeoutError, ConnectionError)) or (
                diagnostic.get('failureKind') in ('timeout', 'connection')
                or diagnostic.get('httpStatus') in (408, 429, 500, 502, 503, 504))
            if index or not transient:
                raise
        finally:
            elapsed = round((time.perf_counter() - call_started) * 1000, 2)
            from .timing import timing_event, append_timing
            event = timing_event(turn, 'reviewModel', elapsed,
                                 includedIn='publication' if phase == 'DOCUMENT' else 'queryReview',
                                 phase=phase, inputChars=input_chars, retry=index > 0,
                                 errorCode=error_code, status='FAILED' if error_code else 'COMPLETED')
            def record(state):
                append_timing(state, event)
                calls = state.setdefault('reviewModelCalls', [])
                calls.append({'phase': phase, 'inputChars': input_chars, 'elapsedMs': elapsed,
                              'retry': index > 0, 'errorCode': error_code})
                del calls[:-64]
            progress(turn, record)
            logging.getLogger(__name__).info('审查模型请求 提问=%s 阶段=%s 重试=%s 材料字符=%d 耗时毫秒=%.2f 错误=%s',
                turn_key(turn), phase, index > 0, input_chars, elapsed, error_code)
