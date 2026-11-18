"""Small business receipts for the successful-empty presentation contract."""
from copy import deepcopy
from analysis_fixtures import CONTEXT
from analysis_extension.review_materials import business_table_view


def attach_empty_business_view(evidence):
    for source in evidence['sources']:
        rows = [{'factId': f'empty-{i}', 'title': '净利润', 'measureCode': 'ZB_JE2',
                 'dimensions': {'MD_ORG': 'org', 'MD_SCENE': 'ACTUAL', 'DATATIME': '2026N0001'},
                 'dimensionNames': {'MD_ORG': '合成机构', 'MD_SCENE': '实际数'},
                 'value': None, 'valueType': 'DECIMAL_STRING', 'valueState': 'NO_DATA', 'unit': '元'}
                for i, _ in enumerate(source.get('facts', []))]
        receipt = {'status': 'SUCCEEDED', 'data': {'purpose': 'QUERY', 'complete': True,
            'question': '查询2026年净利润实际数', 'targets': [{'id': 'profit', 'label': '净利润'}],
            'resultSets': [{'resultSetId': 'result', 'targetId': 'profit', 'context': {
                'taskName': '合成任务', 'formName': '合成利润表', 'period': '2026N0001',
                'effectiveDimensions': {'MD_ORG': 'org', 'MD_SCENE': 'ACTUAL'},
                'dimensionNames': {'MD_ORG': '合成机构', 'MD_SCENE': '实际数'}}, 'rows': rows}]}}
        source['source']['businessTableView'] = business_table_view(receipt)
    return evidence
