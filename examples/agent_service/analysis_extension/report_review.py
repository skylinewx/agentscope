"""Version-bound review progress, independent of publication and requirement state."""
from copy import deepcopy
import json

from .models import digest, now
from .review_request import make_review_request, review_material


def request_identity(turn):
    return digest({key: turn.state.get(key) for key in
                   ('turnKey', 'request', 'goal', 'queryIntent', 'analysisRequirement', 'followupScope')})


def begin_review(turn, analysis_ref, document_key, payloads, prompt):
    from .turn_completion import semantic_review_key
    ref = 'review_' + digest([analysis_ref, document_key])
    record = turn.service.store.get_report_review(turn.service.owner, ref) or {
        'reviewRef': ref, 'analysisRef': analysis_ref, 'documentKey': document_key,
        'requestIdentity': request_identity(turn), 'createdAt': now(), 'parts': []}
    old = {part['key']: part for part in record['parts']}
    parts = []
    for index, payload in enumerate(payloads, 1):
        payload = make_review_request(payload)
        key = semantic_review_key(payload, prompt, turn.service.review_budget)
        # The resumable snapshot includes both canonical evidence and wire text.
        # Distinct host records may intentionally have the same business prose;
        # they must not collide in immutable storage.
        material_ref = 'review_input_' + digest([prompt, payload.stored()])
        turn.service.store.save_review_material(turn.service.owner, material_ref, prompt, payload)
        material = review_material(payload)
        kind = material.get('reviewStage') or material.get('evidencePartition', {}).get('kind', 'COMPLETE_REPORT')
        if material.get('calculationReview'):
            record['calculationReview'] = deepcopy(material['calculationReview'])
        parts.append({**old.get(key, {}), 'key': key, 'materialRef': material_ref, 'index': index, 'kind': kind,
                      'computationKey': material.get('computationKey'),
                      'status': old.get(key, {}).get('status', 'PENDING'), 'inputChars': len(payload)})
    record['parts'] = parts
    record['reviewConfig'] = turn.service.review_budget.identity()
    record['reviewSemanticConfig'] = turn.service.review_budget.semantic_identity()
    record['reviewPolicy'] = digest(prompt)
    turn.state['currentReportReviewRef'] = ref
    return save_review(turn, record)


def save_review(turn, record):
    parts = record['parts']
    record['completedPartCount'] = sum(part['status'] in ('PASS', 'REVISE') for part in parts)
    issues = [deepcopy(issue) for part in parts for issue in part.get('issues', [])]
    for issue in record.get('calculationReview', {}).get('issues', []):
        if issue not in issues:
            issues.append(deepcopy(issue))
    record['issues'] = issues
    record['coverage'] = [{**deepcopy(item), 'stage': part['kind']}
                          for part in parts for item in part.get('coverage', [])]
    record['status'] = ('REVISE' if any(item.get('severity') != 'ADVISORY' for item in issues)
                        else 'PASS' if parts and all(part['status'] == 'PASS' for part in parts)
                             and record.get('calculationReview', {}).get('status', 'PASS') == 'PASS'
                        else 'PARTIAL' if record['completedPartCount'] else 'PENDING')
    record['updatedAt'] = now()
    return turn.service.store.save_report_review(turn.service.owner, record['reviewRef'], record['analysisRef'], record)


def status_text(delivery):
    if delivery.get('semanticReview') == 'REVISE':
        return '当前成果包含需修正的问题；受影响结论尚不能作为确定结果，请查看审核意见。'
    mode = delivery.get('mode')
    if mode == 'PENDING_REVIEW':
        return '当前成果已保存；程序检查已通过，AI 审核尚未全部完成。'
    if mode == 'REVIEWED_FALLBACK':
        return '已交付同一范围内可用的已审版本；较新候选尚未完成交付校验，未替换这份报告。'
    return '此报告已完成必要审查。' if delivery.get('semanticReview') == 'PASS' else '此报告未登记完整审查结论。'


def current_delivery(store, owner, delivery):
    result = deepcopy(delivery)
    record = store.get_report_review(owner, result.get('reviewRef', ''))
    if record and record['analysisRef'] == result['analysisRef']:
        result['review'] = record
        result['semanticReview'] = record['status']
        result['calculationReview'] = deepcopy(record.get('calculationReview'))
        final_parts = [part for part in record['parts'] if part['kind'] != 'CALCULATION']
        result['finalReview'] = ('REVISE' if any(part['status'] == 'REVISE' for part in final_parts)
                                else 'PASS' if final_parts and all(part['status'] == 'PASS' for part in final_parts)
                                else 'PENDING')
        if record['status'] == 'REVISE': result['mode'] = 'REQUIRES_REVISION'
        elif record['status'] == 'PASS' and result['mode'] in ('PENDING_REVIEW', 'REQUIRES_REVISION'): result['mode'] = 'REVIEWED_AFTER_RESUME'
        elif record['status'] in ('PENDING', 'PARTIAL'): result['mode'] = 'PENDING_REVIEW'
    newer = result.get('newerCandidate')
    if newer:
        newer_record = store.get_report_review(owner, newer.get('reviewRef', ''))
        if newer_record and newer_record['analysisRef'] == newer['analysisRef']:
            newer['review'] = newer_record
    result['notice'] = status_text(result)
    return result


def review_summary(delivery):
    """Expose the registered verdict and its business findings alongside the report link."""
    review = delivery.get('review') or {}
    lines = [status_text(delivery)]
    for key, label in (('calculationReview', '计算专项'), ('finalReview', '报告内容')):
        state = delivery.get(key)
        if isinstance(state, dict):
            state = state.get('status')
        wording = {'PASS': '已通过', 'REVISE': '需修正', 'PENDING': '待审', 'UNAVAILABLE': '尚未完成'}.get(state)
        if wording:
            lines.append(label + wording + '。')
    issues = sorted(review.get('issues', []), key=lambda item: item.get('severity') == 'ADVISORY')
    findings = list(dict.fromkeys(str(item.get('message') or item.get('statement') or '').strip() for item in issues))
    findings = [item for item in findings if item]
    if len(findings) > 3:
        lines.append(f'共有 {len(findings)} 条意见，完整记录见报告。')
    return ''.join(lines) + (('\n\n' + '\n'.join('- ' + item for item in findings[:3])) if findings else '')


def review_notice_html(delivery):
    """Status chrome outside the frozen report iframe; report template unchanged."""
    from html import escape
    labels = {'SOURCE_AND_DELIVERY': '来源、范围及报告内容', 'COMPLETE_PYTHON_EXECUTIONS': '完整输入与 Python 计算',
              'COMPLETE_REPORT': '报告与计算依据', 'REPORT_WITH_REVIEWED_COMPUTATIONS': '报告内容（复用已审计算）',
              'CALCULATION': '计算专项审核', 'FINAL_BUSINESS': '最终内容确认'}
    states = {'PASS': '已完成，本范围未发现阻断问题', 'REVISE': '存在需修正问题',
              'PENDING': '尚未完成', 'UNAVAILABLE': '调用未完成，尚无审查结论'}
    lines = [f'<aside role="status" data-analysis-review="{escape(delivery.get("semanticReview", "NOT_REVIEWED"))}">',
             '<strong>' + escape(status_text(delivery)) + '</strong>',
             '<p>当前成果与审核状态分别记录。</p>']
    def details(review, title):
        parts = (review or {}).get('parts', [])
        if not parts: return
        lines.append('<details><summary>' + escape(title) + '</summary><ul>')
        for part in parts:
            lines.append('<li>' + escape(labels.get(part['kind'], part['kind']) + '：' + states.get(part['status'], part['status'])) + '</li>')
            for item in part.get('coverage', []):
                label = {'COVERED': '已覆盖', 'MISSING': '遗漏', 'SOURCE_LIMITED': '来源能力受限'}.get(item['status'], item['status'])
                lines.append('<li>' + escape(item['requirement'] + '：' + label
                                            + ('（' + item['location'] + '）' if item.get('location') else '')) + '</li>')
            for issue in part.get('issues', []):
                text = str(issue.get('message', issue.get('statement', issue.get('code', ''))))
                if issue.get('evidenceRefs'): text += '（依据：' + '、'.join(issue['evidenceRefs']) + '）'
                lines.append('<li>' + escape(text) + '</li>')
        lines.append('</ul></details>')
    details(delivery.get('review'), '当前成果的审核详情')
    calculation = delivery.get('calculationReview') or (delivery.get('review') or {}).get('calculationReview')
    if calculation and not any(part['kind'] == 'CALCULATION' for part in (delivery.get('review') or {}).get('parts', [])):
        details({'parts': [{'kind': 'CALCULATION', **calculation}]}, '已保存的计算审核意见')
    newer = delivery.get('newerCandidate')
    if newer:
        lines.append('<p>较新候选未被采用；其审查意见不代表所交付版本存在同样问题。</p>')
        lines.append('<p>较新候选程序检查：' + ('通过' if newer.get('programChecks') == 'PASS' else '未通过') + '</p>')
        details(newer.get('review'), '较新候选的审查详情')
    return ''.join(lines) + '</aside>'


def attach_review_notice(html, delivery):
    import re
    start, end = '<!--analysis-review-start-->', '<!--analysis-review-end-->'
    notice = start + review_notice_html(delivery) + end
    if start in html:
        return re.sub(re.escape(start) + r'.*?' + re.escape(end), lambda _: notice, html, count=1, flags=re.S)
    return html.replace('<body>', '<body>' + notice, 1)


def candidate_proof(turn, proof, diagnostics):
    from .review_control import progress
    proof = deepcopy(proof)
    ref = turn.state.get('currentReportReviewRef')
    review = turn.service.store.get_report_review(turn.service.owner, ref) if ref else None
    if review and review['analysisRef'] == proof['analysisRef']:
        proof['reviewRef'] = ref
    else:
        verdict = diagnostics.get('semanticReview', {})
        if verdict.get('status') in ('PASS', 'REVISE', 'UNAVAILABLE', 'NOT_REVIEWED'):
            ref = 'review_' + digest([proof['analysisRef'], proof['contentDigest'], proof['evidenceDigest'], request_identity(turn)])
            part = {'key': ref, 'index': 1, 'kind': 'COMPLETE_REPORT',
                    'status': 'PENDING' if verdict['status'] == 'NOT_REVIEWED' else verdict['status'],
                    'issues': deepcopy(verdict.get('issues', [])), 'coverage': deepcopy(verdict.get('coverage', []))}
            if verdict.get('error'): part['failure'] = deepcopy(verdict['error'])
            record = {'reviewRef': ref, 'analysisRef': proof['analysisRef'], 'requestIdentity': request_identity(turn),
                      'createdAt': now(), 'parts': [part]}
            save_review(turn, record)
            proof['reviewRef'] = ref
    proof['requestIdentity'] = request_identity(turn)
    if proof.get('reviewRef'):
        turn.state['currentReportReviewRef'] = proof['reviewRef']
    proof['reviewResult'] = deepcopy(diagnostics.get('semanticReview', {}))
    proof['programChecks'] = 'PASS' if not proof.get('publicationCheckCode') else 'FAILED'
    def save(state):
        versions = state.setdefault('publicationCandidates', [])
        versions[:] = [item for item in versions if item['previewRef'] != proof['previewRef']]
        versions.append(proof)
        state['lastPublicationPreview'] = proof
    progress(turn, save)
    return proof


async def resume_review(service, analysis_ref, action='RESUME'):
    """A view reads the stored verdict; a new business assessment gets its own identity."""
    import asyncio
    if not hasattr(service, '_report_review_lock'):
        service._report_review_lock = asyncio.Lock()
    async with service._report_review_lock:
        return await _review_frozen_report(service, analysis_ref, action)


async def _review_frozen_report(service, analysis_ref, action):
    import time
    from jsonschema import validate
    from .models import AnalysisError
    from .review_output import ReviewOutputUnavailable
    from .review_control import call_review_model, reserve_review, finish_review
    from .turn_completion import ANALYSIS_REVIEW_SCHEMA, semantic_review_key, REVIEW_PROMPT
    await service.authorize()
    artifact = service.store.get_analysis(service.owner, analysis_ref)
    delivery = artifact.get('reportDelivery')
    if not delivery or artifact['publicationState'] != 'PUBLISHED':
        raise AnalysisError('REPORT_REVIEW_REQUIRED', '续审需要已交付且绑定审查记录的报告版本。')
    record = service.store.get_report_review(service.owner, delivery.get('reviewRef', ''))
    if not record or service.turn is None or (action != 'VIEW' and not service.material_reviewer):
        raise AnalysisError('REVIEW_UNAVAILABLE', '当前未配置可用的审查恢复入口。')
    if action == 'VIEW':
        return _review_result(service, artifact, record, action)
    from .review_business_projection import report_source_tables
    source_tables = report_source_tables(artifact.get('facts', []))
    from .review_budget import ReviewBudgetConfig
    previous_semantics = record.get('reviewSemanticConfig') or ReviewBudgetConfig(**record.get('reviewConfig', {})).semantic_identity()
    config_changed = (previous_semantics != service.review_budget.semantic_identity()
                      or record.get('reviewPolicy') != digest(REVIEW_PROMPT))
    if config_changed:
        history = deepcopy(record.get('priorPolicyReviews', []))
        history.append({key: deepcopy(record.get(key)) for key in
                        ('reviewConfig', 'reviewPolicy', 'status', 'parts', 'calculationReview', 'updatedAt')})
        computation = next((part.get('computationKey') for part in record['parts'] if part.get('computationKey')), None)
        if computation:
            from .turn_completion import analysis_review_payloads, _review_json
            from .computation_review import computation_key, calculation_material
            from .review_encoding import unpack_review_records
            from .review_budget import use_budget
            original = service.store.get_review_material(service.owner, 'review_source_' + computation)
            material = unpack_review_records(json.loads(original['payload']))
            new_key = computation_key(material, REVIEW_PROMPT, service.review_budget)
            service.store.save_review_material(service.owner, 'review_source_' + new_key,
                                               REVIEW_PROMPT, _review_json(material))
            with use_budget(service.review_budget):
                payloads = analysis_review_payloads(calculation_material(material, new_key))
            for previous in record['parts']:
                if previous['kind'] == 'CALCULATION': continue
                frozen = service.store.get_review_material(service.owner, previous['materialRef'])
                final_material = review_material(frozen['payload'])
                final_material.update(computationKey=new_key,
                                      calculationReview={'status': 'PENDING', 'materialKey': new_key})
                payloads.append(make_review_request(final_material))
            record = begin_review(service.turn, analysis_ref, record['documentKey'], payloads, REVIEW_PROMPT)
        for part in record['parts']:
            part.update(status='PENDING', issues=[], coverage=[])
        record.pop('calculationReview', None)
        record['reviewConfig'] = service.review_budget.identity()
        record['reviewSemanticConfig'] = service.review_budget.semantic_identity()
        record['reviewPolicy'] = digest(REVIEW_PROMPT)
        record['priorPolicyReviews'] = history[-4:]
        save_review(service.turn, record)
    if action == 'RECHECK':
        refresh = digest([service.turn.state['turnKey'], analysis_ref, delivery['contentDigest'], review_policy_identity(record)])
        if refresh != record.get('businessRecheck'):
            for part in record['parts']:
                if part['kind'] != 'CALCULATION' and not part.get('materialRef'):
                    raise AnalysisError('REVIEW_MATERIAL_UNAVAILABLE', '未找到该正文的冻结审查材料，无法生成新的复核结论。')
            record.setdefault('previousBusinessReviews', []).append({
                'reviewedAt': record.get('updatedAt'), 'status': record['status'],
                'issues': deepcopy(record.get('issues', [])), 'coverage': deepcopy(record.get('coverage', []))})
            record['businessRecheck'] = refresh
            for part in record['parts']:
                if part['kind'] != 'CALCULATION':
                    original_ref = part.setdefault('originalBusinessMaterialRef', part['materialRef'])
                    original = service.store.get_review_material(service.owner, original_ref)
                    material = review_material(original['payload'])
                    material.pop('revisionFocus', None)
                    material['businessReviewRequest'] = service.turn.state['request']
                    payload = make_review_request(material)
                    prompt = REVIEW_PROMPT if config_changed else original['prompt']
                    ref = 'review_input_' + digest([prompt, payload.stored()])
                    service.store.save_review_material(service.owner, ref, prompt, payload)
                    part.update(materialRef=ref, inputChars=len(payload))
                    part.update(status='PENDING', issues=[], coverage=[])
                    part.pop('failure', None)
            save_review(service.turn, record)
    def part_key(part, payload, prompt):
        key = semantic_review_key(payload, prompt, service.review_budget)
        if record.get('businessRecheck') and part['kind'] != 'CALCULATION':
            return digest([key, record['businessRecheck']])
        return key
    def refresh_completed_calculation():
        computation_parts = [item for item in record['parts'] if item['kind'] == 'CALCULATION']
        keys = {item.get('computationKey') for item in computation_parts}
        if (not computation_parts or len(keys) != 1 or None in keys
                or any(item['status'] not in ('PASS', 'REVISE') for item in computation_parts)):
            return
        findings = [issue for item in computation_parts for issue in item.get('issues', [])]
        key = next(iter(keys))
        proof = service.store.save_semantic_review(service.owner, key,
            {'kind': 'COMPUTATION', 'materialKey': key, 'issues': findings,
             'coverage': [deepcopy(item) for part in computation_parts for item in part.get('coverage', [])],
             'status': 'REVISE' if any(item.get('severity') != 'ADVISORY' for item in findings) else 'PASS'})
        record['calculationReview'] = proof
        # A final review made while calculation was unavailable is a different
        # input from the completed calculation receipt. Invalidate it before
        # saving aggregate progress; the exact new bytes are frozen below.
        from .computation_review import refresh_business_proof
        for part in record['parts']:
            if part['kind'] != 'FINAL_BUSINESS':
                continue
            material = service.store.get_review_material(service.owner, part['materialRef'])
            payload = refresh_business_proof(service.turn, material['payload'])
            prompt = REVIEW_PROMPT if config_changed else material['prompt']
            if part_key(part, payload, prompt) != part['key']:
                part.update(status='PENDING', issues=[], coverage=[])
                part.pop('failure', None)

    # Also reconcile already completed parts from an earlier interrupted
    # resume. Their saved verdicts are sufficient; do not rerun calculation.
    refresh_completed_calculation()
    save_review(service.turn, record)
    started, charged = time.perf_counter(), False
    output_exhausted = False
    try:
        if any(part['status'] not in ('PASS', 'REVISE') for part in record['parts']):
            for part in record['parts']:
                material = service.store.get_review_material(service.owner, part.get('materialRef', part['key']))
                from .computation_review import refresh_business_proof
                payload = refresh_business_proof(service.turn, material['payload'])
                if source_tables and part['kind'] != 'CALCULATION' and part['status'] not in ('PASS', 'REVISE'):
                    source_material = review_material(payload)
                    if source_material.get('reportSourceTables') != source_tables:
                        payload = make_review_request({**source_material, 'reportSourceTables': source_tables})
                if config_changed:
                    # Old revision focus assumes the earlier policy completed
                    # review. A new policy/profile must examine the whole body.
                    value = review_material(payload)
                    value.pop('revisionFocus', None)
                    payload = make_review_request(value)
                prompt = REVIEW_PROMPT if config_changed else material['prompt']
                key = part_key(part, payload, prompt)
                if key != part['key']:
                    part.update(key=key, status='PENDING', issues=[], coverage=[])
                    payload = make_review_request(payload)
                    material_ref = 'review_input_' + digest([prompt, payload.stored()])
                    service.store.save_review_material(service.owner, material_ref, prompt, payload)
                    part.update(materialRef=material_ref, inputChars=len(payload))
                material = {'prompt': prompt, 'payload': payload}
                if part['status'] in ('PASS', 'REVISE'): continue
                verdict = service.store.get_semantic_review(service.owner, part['key'])
                if verdict is None:
                    if not charged:
                        reserve_review(service.turn); charged = True
                    try:
                        verdict = await call_review_model(service.turn,
                            lambda: service.material_reviewer(material['prompt'], material['payload']),
                            phase='CALCULATION' if part['kind'] == 'CALCULATION' else 'DOCUMENT',
                            input_chars=len(material['payload']), round_started=started, material_key=key)
                        validate(verdict, ANALYSIS_REVIEW_SCHEMA)
                        service.store.save_semantic_review(service.owner, part['key'], verdict)
                    except Exception as error:
                        output_exhausted = output_exhausted or isinstance(error, ReviewOutputUnavailable)
                        failure = error.as_dict() if isinstance(error, AnalysisError) else {
                            'code': type(error).__name__, 'message': '本段尚未完成，已保留此前结果。'}
                        part.update(status='UNAVAILABLE', failure=failure)
                        save_review(service.turn, record)
                        if part['kind'] == 'CALCULATION' and not output_exhausted:
                            continue
                        break
                from .formula_check_summary import normalize_review_issue
                from .review_encoding import unpack_review_records
                business_material = review_material(material['payload'])
                issues = [normalize_review_issue(item, business_material) for item in verdict['issues']]
                part.update(status='REVISE' if any(item.get('severity') != 'ADVISORY' for item in issues) else 'PASS',
                            issues=deepcopy(issues), coverage=deepcopy(verdict.get('coverage', [])))
                part.pop('failure', None)
                if part['kind'] == 'CALCULATION' and part.get('computationKey'):
                    refresh_completed_calculation()
                elif part['kind'] == 'FINAL_BUSINESS':
                    record['calculationReview'] = business_material.get('calculationReview', {})
                save_review(service.turn, record)
    finally:
        if charged:
            finish_review(service.turn, (time.perf_counter() - started) * 1000,
                          {'issues': record['issues']} if record['status'] in ('PASS', 'REVISE') else None,
                          unavailable=record['status'] not in ('PASS', 'REVISE'),
                          release_output_only=output_exhausted and record['status'] not in ('PASS', 'REVISE'))
    return _review_result(service, artifact, record, action)


def review_policy_identity(record):
    return [record.get('reviewPolicy'), record.get('reviewSemanticConfig')]


def _review_result(service, artifact, record, action):
    analysis_ref, delivery = artifact['analysisRef'], artifact['reportDelivery']
    metadata = current_delivery(service.store, service.owner, delivery)
    service.turn.state['reportReviewRead'] = True
    registered = service.delivery.ensure(analysis_ref) if service.delivery else {}
    if registered.get('registered'):
        from .review_control import progress
        def remember(saved):
            receipts = saved.setdefault('resumedReportRefs', {})
            identity = {'reviewRef': record['reviewRef'], 'contentDigest': delivery['contentDigest']}
            previous = receipts.get(analysis_ref, {})
            performed = action
            if all(previous.get(key) == value for key, value in identity.items()):
                if action == 'VIEW' or previous.get('action') == 'RECHECK':
                    performed = previous.get('action') or action
            receipts[analysis_ref] = {**identity, 'action': performed}
        progress(service.turn, remember)
    return {'analysisRef': analysis_ref, 'publicationState': 'PUBLISHED', 'reportDelivery': metadata,
            'reviewAction': action, 'reviewNotice': status_text(metadata), 'delivery': registered}


def choose_candidate(service, artifact):
    if service.turn is None: return None
    from .closeout_content import evidence_digest
    from .review_control import progress
    candidates = progress(service.turn).get('publicationCandidates', [])
    evidence = evidence_digest(artifact)
    identity = request_identity(service.turn)
    applicable = [item for item in candidates if item.get('evidenceDigest') == evidence
                  and item.get('requestIdentity') == identity]
    if not applicable:
        return None
    # Current progress is the current candidate. Never silently replace it with
    # an older approved report, even when its review or rendering failed.
    candidate = deepcopy(applicable[-1])
    if candidate.get('programChecks') != 'PASS':
        return None  # Preserve the preview and diagnostics; unsafe scripts are not published.
    review = service.store.get_report_review(service.owner, candidate.get('reviewRef', ''))
    candidate['effectiveReview'] = review
    status = (review or {}).get('status', candidate.get('semanticReview'))
    candidate['semanticReview'] = status
    mode = 'REVIEWED' if status == 'PASS' else 'REQUIRES_REVISION' if status == 'REVISE' else 'PENDING_REVIEW'
    return {**candidate, 'mode': mode}


def _blocking_issues(candidate):
    review = candidate.get('effectiveReview') or candidate.get('reviewResult') or {}
    issues = [issue for issue in review.get('issues', []) if issue.get('severity') != 'ADVISORY']
    if not issues and (candidate.get('semanticReview') == 'REVISE' or review.get('status') == 'REVISE'):
        return [{'impact': 'UNKNOWN'}]
    return issues


def _does_not_invalidate(approved, later):
    """Only a located report-only error absent from an approved text is isolated."""
    import re
    normalize = lambda text: re.sub(r'\s+', ' ', text).strip()
    old = normalize(' '.join(str(value) for value in approved.get('documentTexts', {}).values()))
    new = normalize(' '.join(str(value) for value in later.get('documentTexts', {}).values()))
    for issue in _blocking_issues(later):
        quote = normalize(issue.get('statement', ''))
        if issue.get('impact') != 'REPORT' or not quote or not old or quote not in new or quote in old:
            return False
    return True
