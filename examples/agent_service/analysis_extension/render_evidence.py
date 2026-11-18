"""复用宿主对冻结页面的真实观察，避免把动态占位符当作最终展示。"""
from .models import AnalysisError

TEXT_LIMIT = 262144


def snapshot(text):
    """过长时明确未纳入正文，不用截断的前半页代表完整页面。"""
    return {'text': text if len(text) <= TEXT_LIMIT else '', 'complete': len(text) <= TEXT_LIMIT}


def validate_snapshot(value):
    if (not isinstance(value, dict) or set(value) != {'text', 'complete'}
            or not isinstance(value['text'], str) or len(value['text']) > TEXT_LIMIT
            or type(value['complete']) is not bool or not value['complete'] and value['text']):
        raise AnalysisError('INVALID_PREVIEW_CHECK', '宿主页面文字观察无效。')
    return dict(value)


def review_text(turn, artifact, source_text):
    """只接纳当前内容摘要、当前归属下的宿主实测；页面/人工上传内容不充当观察。"""
    document = artifact.get('freeDocument', {})
    read_checks = getattr(turn.service.store, 'document_checks', None)
    records = read_checks(turn.service.owner, artifact['analysisRef'], document['contentDigest']) if (
        callable(read_checks) and artifact.get('analysisRef') and document.get('contentDigest')) else []
    texts, widths = [], []
    for record in records:
        observed = record.get('hostRenderedText', {})
        if (record.get('runner') not in ('headless-service', 'workspace-browser') or record.get('contentDigest') != document.get('contentDigest')
                or record.get('blocking') or observed.get('complete') is not True or not observed.get('text')):
            continue
        if observed['text'] not in texts:
            texts.append(observed['text'])
        widths.append(record['viewport']['width'])
    if not texts:
        result = {'documentText': source_text, 'documentTextBasis': 'STATIC_SOURCE',
                  'documentObservation': '尚无当前页面的完整实测文本；下列绑定内容是冻结业务数据，不能据此断言所有内容已在可见页面正确展示。'}
        marker = '<script id="analysis-host-data" type="application/json">'
        if marker in document.get('html', ''):
            import json
            payload = json.loads(document['html'].split(marker, 1)[1].split('</script>', 1)[0])
            result['documentBindingContent'] = {'values': payload['values'], 'sets': payload['sets']}
            result['documentTextBasis'] = 'STATIC_AND_BOUND_DATA'
        return result
    result = {'documentText': '\n\n'.join(texts), 'documentTextBasis': 'HOST_RENDERED',
              'documentTextViewports': widths}
    if source_text and source_text.split() != result['documentText'].split():
        # 源码文字用于检查隐藏/折叠分支的陈述，不能据其中占位符否定实测数值。
        result['documentSourceText'] = source_text
    return result
