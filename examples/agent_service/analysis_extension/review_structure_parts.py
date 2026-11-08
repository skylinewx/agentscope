"""Partition structural context without reducing numerical facts or computation inputs."""
from copy import deepcopy


def structure_review_parts(payload, encode, limit, max_parts):
    structures = payload.get('structureEvidence', [])
    if not structures or not any(item.get('cells') for item in structures):
        return None
    cells = [(index, cell) for index, item in enumerate(structures) for cell in item.get('cells', [])]
    base = {**payload, 'structureEvidence': [{**item, 'cells': [],
            'reviewCellCount': len(item.get('cells', []))} for item in structures]}

    def material(selected, index=0, count=0):
        result = {**base, 'structureEvidence': deepcopy(base['structureEvidence'])}
        for source_index, cell in selected:
            result['structureEvidence'][source_index]['cells'].append(cell)
        result['evidencePartition'] = {
            'kind': 'STRUCTURE_CONTEXT', 'index': index, 'count': count,
            'allTargetsPreserved': True, 'completeCalculationInputs': True,
            'totalStructureCells': len(cells), 'includedStructureCells': len(selected),
            'meaning': '每段保留全部数值事实、计算输入及结果；仅表结构单元格分段，全部分段均须核对。'
                       '未列出的结构格及跨段引用在其他分段中，不表示来源缺失；来源 coverage/status 保持原值。'}
        return result

    # If numerical dependencies themselves are too large, retain the existing
    # calculation-dependency partitioning and its explicit capacity failure.
    if len(encode(material([]))) > limit - 500:
        return None
    parts, offset = [], 0
    while offset < len(cells):
        low, high, fit = 1, len(cells) - offset, 0
        while low <= high:
            middle = (low + high) // 2
            if len(encode(material(cells[offset:offset + middle]))) <= limit - 500:
                fit, low = middle, middle + 1
            else:
                high = middle - 1
        if not fit or len(parts) >= max_parts:
            return None
        parts.append(cells[offset:offset + fit])
        offset += fit
    return [encode(material(part, index + 1, len(parts))) for index, part in enumerate(parts)]
