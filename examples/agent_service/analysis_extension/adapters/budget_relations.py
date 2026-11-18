"""Source-backed direct relationships, independent of business aggregation rules."""
from .budget_v2 import receipt_from_capture, data_context
from ..actions import source_identity
from ..models import AnalysisError, digest, decimal_value, decimal_text, precise_sum
from .budget_discovery import entity_rows


def direct_outputs(captures, receipts, diagnostics=None):
    """Normalize current plan and standalone traversal contracts, not IRI syntax."""
    pages = {}
    for ref, capture in captures.items():
        receipt, request = receipts[ref], capture['request']
        steps = {s.get('id'): s for s in request.get('plan', {}).get('steps', [])}
        returned = {s.get('stepId'): s for s in (receipt.get('data') or {}).get('stepResults', [])}
        for sid, step in steps.items():
            if (step.get('operation') != 'TRAVERSE_RELATION' or step.get('traversalScope') != 'DIRECT'
                    or step.get('maximumDepth', 1) != 1 or not step.get('capabilityId') or sid not in returned): continue
            inputs = [steps.get(key, {}) for key in step.get('inputs', [])]
            if not inputs: continue
            # 根身份来自已执行输入步骤，允许 LIST/筛选后的集合，不靠 IRI 或模型代码猜测。
            roots = set()
            for source in inputs:
                output = returned.get(source.get('id'), {})
                roots.update(row['resourceIri'] for row in output.get('rows', []) if row.get('resourceIri'))
                if source.get('operation') == 'RESOLVE_ENTITY':
                    roots.update(source.get('resourceIris', []))
            yield ref, sid, step['capabilityId'], roots, returned[sid]['rows'], (
                receipt.get('completeness', {}).get('enumerationExhausted') is True and returned[sid].get('exactness') == 'EXACT'), [ref]
        if capture.get('originalToolName') != 'traverse_relation': continue
        data = receipt.get('data') or {}
        if request.get('traversalScope') != 'DIRECT': continue
        if (request.get('traversalMaximumDepth', 1) != 1
                or data.get('scope') != 'DIRECT' or data.get('maximumDepth') != 1
                or not request.get('resourceIri') or request['resourceIri'] != data.get('rootResourceIri')
                or not request.get('capabilityId') or request['capabilityId'] != data.get('capabilityId')):
            if diagnostics is not None:
                diagnostics.append({'captureRef': ref, 'code': 'RELATION_CONTRACT_MISMATCH',
                                    'message': '关系回执的根节点、能力、范围或有效深度与请求不一致。'})
            continue
        normalized_request = {k:v for k,v in request.items() if k not in ('requestId','traversalCursor')}
        normalized_request['traversalMaximumDepth'] = 1
        identity = digest({'source': source_identity(capture), 'context': receipt.get('context'),
                           'querySessionId': receipt.get('querySessionId'),
                           'request': normalized_request})
        pages.setdefault(identity, []).append((ref, request, data, receipt))
    for group in pages.values():
        for first in [p for p in group if not p[1].get('traversalCursor')]:
            chain, seen, complete = [], set(), False
            current = first
            while current and current[0] not in seen:
                seen.add(current[0]); chain.append(current)
                page = current[2].get('page') or {}
                cursor = page.get('nextCursor')
                if cursor is None:
                    complete = (page.get('enumerationExhausted') is True and not page.get('stopReasons')
                                and current[3].get('completeness', {}).get('enumerationExhausted') is True)
                    break
                candidates = [p for p in group if p[1].get('traversalCursor') == cursor]
                # Ambiguous continuations must not silently select one result.
                current = candidates[0] if len(candidates) == 1 else None
            yield first[0], None, first[1]['capabilityId'], {first[1]['resourceIri']}, [r for p in chain for r in p[2].get('rows', [])], complete, [p[0] for p in chain]


def captured_relations(captures, diagnostics=None):
    receipts = {ref: receipt_from_capture(c) for ref, c in captures.items()}
    identities = {}
    for ref, receipt in receipts.items():
        origin = digest({'source': source_identity(captures[ref]), 'context': data_context(receipt['context'])})
        for row in entity_rows(receipt):
            if row.get('resourceIri') and row.get('key'):
                identities.setdefault((origin, row['resourceIri']), set()).add((row['key'], row.get('modelCode')))
    result = []
    for ref, sid, capability, roots, rows, complete, source_refs in direct_outputs(captures, receipts, diagnostics):
        capture = captures[ref]
        receipt = receipts[ref]
        origin = digest(source_identity(capture))
        identity = digest({'source': source_identity(capture), 'context': data_context(receipt['context'])})
        if not roots or any(r.get('rootResourceIri') not in roots or r.get('depth') != 1
                            or not r.get('resourceIri') or not r.get('key') for r in rows):
            if diagnostics is not None:
                diagnostics.append({'captureRef': ref, 'code': 'RELATION_ROWS_INVALID', 'message': '关系行缺少身份或不属于请求的直接根集合。'})
            continue
        for root in sorted(roots):
            parent = identities.get((identity, root), set())
            if len(parent) != 1:
                if diagnostics is not None:
                    diagnostics.append({'captureRef': ref, 'code': 'RELATION_ROOT_UNRESOLVED', 'resourceIri': root,
                                        'message': '关系根节点缺少唯一的来源身份，请读取根实体信息。'})
                continue
            children = [r['key'] for r in rows if r['rootResourceIri'] == root]
            parent_id, parent_model = next(iter(parent))
            self_link = any(row['key'] == parent_id and row.get('modelCode') == parent_model
                            for row in rows if row['rootResourceIri'] == root)
            if len(children) != len(set(children)) or self_link:
                if diagnostics is not None:
                    diagnostics.append({'captureRef': ref, 'code': 'RELATION_IDENTITY_CONFLICT',
                                        'message': '关系成员身份重复或出现同模型自关联，不能用作完整关系证据。'})
                continue
            item = {'captureRef': ref, 'captureRefs': source_refs, 'stepId': sid, 'sourceOrigin': origin,
                    'context': receipt.get('context'), 'capabilityId': capability,
                    'parentId': parent_id, 'parentModelCode': parent_model, 'childIds': children,
                    'childModelCodes': {r['key']: r.get('modelCode') for r in rows if r['rootResourceIri'] == root}, 'complete': complete}
            item['relationRef'] = 'relation_' + digest(item)[:24]
            item['dataContext'] = data_context(receipt['context'])
            result.append(item)
    return result
