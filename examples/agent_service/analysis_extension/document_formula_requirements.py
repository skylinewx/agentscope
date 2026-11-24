"""Enforce already declared formula work; never infer intent from user words."""
from .models import AnalysisError
from .scalar_references import reference_id
from .document_coverage import WATERFALL_BINDING_HELP


def require_formula_datasets(artifact, datasets):
    values={value['derivedId']:value for value in artifact.get('derivedValues',[])}
    updates={item['taskId']:item['refs'] for item in artifact.get('workRefs',[])}
    required=set()
    targets=set()
    for task in artifact.get('plan',{}).get('workPlan',[]):
        if task.get('kind')!='COMPUTATION': continue
        if task.get('operator')!='FORMULA_CHECK' and task.get('requiredEvidence')!='FORMULAS': continue
        targets.update(task.get('targetRefs',[]))
        required.update(reference_id(ref) for ref in updates.get(task['taskId'],task.get('refs',[])))
    # A prepared formula at a declared target remains required even when the
    # page binds only its source amount. Unrelated cached formulas add no work.
    selected_targets={values[ref].get('formulaTrace',{}).get('targetFactRef') for ref in required if ref in values}
    for target in targets-selected_targets:
        candidates=[ref for ref,value in values.items() if value.get('operator')=='FORMULA_CHECK'
                    and value.get('formulaTrace',{}).get('targetFactRef')==target]
        if len(candidates)==1: required.add(candidates[0])
    represented={}
    for dataset in datasets.values():
        if dataset.get('kind')=='FORMULA':
            represented.setdefault(dataset['valueRef'],set()).update(node['factRef'] for node in dataset['nodes'])
    missing=[]
    for ref in sorted(required):
        value=values.get(ref,{})
        if value.get('operator')!='FORMULA_CHECK': continue
        nodes={node['factRef'] for node in value.get('formulaTrace',{}).get('nodes',[])
               if node.get('expression') and not node.get('boundary')}
        absent=nodes-represented.get(ref,set())
        if absent:
            missing.append({'valueRef':ref,'targetFactRef':value['formulaTrace']['targetFactRef'],
                            'missingNodeRefs':sorted(absent)})
    if missing:
        raise AnalysisError('DOCUMENT_FORMULA_DATASET_REQUIRED',
            '本题已确认的公式构成尚未完整关联到页面；只绑定结果金额不能代替已取得的构成。',
            {'path':'/document/datasets','missingFormulas':missing,'requiresBuild':False,
             'hint':'为所列 valueRef 绑定 FORMULA dataset。省略 nodeRefs 可保留已返回的全部计算层。'
                    '多项加减节点同时绘制横向瀑布图。' + WATERFALL_BINDING_HELP})
