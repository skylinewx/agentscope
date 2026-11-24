"""Check review output consistency, without pretending to judge business semantics."""
from .models import AnalysisError


def validate_coverage(verdict, material):
    coverage = verdict.get('coverage', [])
    if not coverage:
        raise AnalysisError('REVIEW_COVERAGE_INVALID', '审查没有返回本阶段的关键要求覆盖结论。')
    if any(item['status'] == 'MISSING' for item in coverage) and not any(
            item.get('severity') == 'BLOCKING' for item in verdict.get('issues', [])):
        raise AnalysisError('REVIEW_COVERAGE_INVALID', '审查标记关键要求遗漏，却未给出对应实质问题。')
    # Coverage is a model judgment. This check only validates referenced identities.
    known = set()
    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith(('Ref', 'Id')) and isinstance(child, str):
                    known.add(child)
                elif key.endswith(('Refs', 'Ids')) and isinstance(child, list):
                    known.update(ref for ref in child if isinstance(ref, str))
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    # Readable tables contain the same exact references in Markdown cells.
    from .readable_material import restore_material
    restored = restore_material(material) if material.get('materialFormat') else material
    collect(restored)
    objectives = {o['number']: o for o in restored.get('objectiveCoverage', [])}
    visible = {v['derivedId'] for v in restored.get('derived', []) if v.get('derivedId')}
    applicable = {i: set(o.get('formulaRefs', [])) & visible for i, o in objectives.items()
                  if o['capability'] == 'DATA_COMPOSITION' and not o.get('exploratory')}
    checked = set()
    for row in coverage:
        number = row.get('objective')
        if number is None:
            continue
        if number not in objectives:
            raise AnalysisError('REVIEW_COVERAGE_INVALID', '核对对象编号必须来自本阶段材料。')
        if row['status'] == 'COVERED' and applicable.get(number) and not (applicable[number] & set(row['evidenceRefs'])):
            raise AnalysisError('REVIEW_COVERAGE_INVALID', f'目标 {number} 的构成结论请引用该对象的来源公式结果。若正文遗漏构成，请按实际情况纠正覆盖判断。')
        checked.add(number)
    missing = [n for n, refs in applicable.items() if refs and n not in checked]
    if missing:
        raise AnalysisError('REVIEW_COVERAGE_INVALID', '请为本阶段可核对的数据构成目标给出覆盖结论并填写目标编号 ' + '、'.join(map(str, missing)))
    unknown, invalid_positions = [], {}
    for index, item in enumerate(coverage):
        invalid = [pos for pos, ref in enumerate(item['evidenceRefs']) if ref not in known]
        if invalid:
            invalid_positions[index] = invalid
            unknown.extend(item['evidenceRefs'][pos] for pos in invalid)
    if invalid_positions:
        error = AnalysisError('REVIEW_COVERAGE_INVALID', '覆盖结论的数据定位无法对应本次材料，请使用实际数据行、脚本或结果的位置。',
                              {'unknownEvidenceRefs': list(dict.fromkeys(unknown))[:12], 'coverageIndexes': list(invalid_positions)})
        error.invalid_positions = invalid_positions
        raise error
