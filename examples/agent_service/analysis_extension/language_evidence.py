"""Host-owned evidence binding. Models supply meaning, not reconstructed quotations."""
from .models import AnalysisError, digest


def bind(turn, request):
    draft = request.get('intentDraft') or {}
    sources = [('CURRENT_QUESTION', request.get('question', '')),
               ('CONFIRMED_CONTEXT', request.get('contextText', ''))]
    records = []

    def anchor(text, path):
        for kind, source in sources:
            if text and text in source:
                start = source.index(text)
                records.append({'path': path, 'source': kind, 'start': start,
                                'end': start + len(text), 'text': text})
                return text
        raise AnalysisError('QUERY_DRAFT_EVIDENCE_INVALID',
                            '业务文字没有本轮原文或已确认历史依据。', {'path': path, 'text': text})

    phrases = [(f'sharedConditions/{i}', p) for i, p in enumerate(draft.get('sharedConditions', []))]
    phrases += [(f'targets/{t["id"]}/businessPhrases/{i}', p) for t in draft.get('targets', [])
                for i, p in enumerate(t.get('businessPhrases', []))]
    for path, phrase in phrases:
        phrase['sourceText'] = anchor(phrase.get('text'), path)
    for index, scope in enumerate(draft.get('dimensionScopes', [])):
        path = f'dimensionScopes/{scope.get("id", index)}'
        selector = scope.get('selector') or {}
        if scope.get('confirmedSource'):
            # The host already restored this exact committed selector. Its quote may
            # contain obsolete years/metrics, so never require it in current context.
            confirmed = (turn.state.get('inheritedDimensionScopes', []) if turn is not None else [])
            if not any(item.get('confirmedSource') == scope['confirmedSource']
                       and item.get('draft', {}).get('selector') == selector
                       and item.get('draft', {}).get('sourceText') == scope.get('sourceText') for item in confirmed):
                raise AnalysisError('QUERY_DRAFT_EVIDENCE_INVALID', '继承范围必须引用宿主已确认的历史条件。',
                                    {'path': path + '/confirmedSource'})
            records.append({'path': path, 'source': 'CONFIRMED_EXECUTION', 'text': scope['sourceText'],
                            'confirmedSource': scope['confirmedSource']})
            continue
        for key, value in [('rootText', selector.get('rootText')),
                           *[(f'members/{i}', value) for i, value in enumerate(selector.get('members', []))]]:
            if value:
                anchor(value, f'{path}/selector/{key}')
        quote = (request.get('question') or '').strip()
        scope['sourceText'] = anchor(quote, path)
    for index, relation in enumerate(draft.get('relations', [])):
        relation['sourceText'] = anchor((request.get('question') or '').strip(), f'relations/{index}')
        root = (relation.get('hierarchy') or {}).get('rootText')
        if root:
            anchor(root, f'relations/{index}/hierarchy/rootText')
    for index, binding in enumerate(request.get('scope', {}).get('bindings', [])):
        anchor(binding.get('text'), f'scope/bindings/{index}/text')
    # This audit stays in host state. It is not another model payload or another semantic declaration.
    if turn is not None:
        turn.state.setdefault('businessEvidence', {})[digest(request)] = records
    return request
