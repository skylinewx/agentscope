"""Bounded exact operators; business rules and sum expectations are explicit."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import re

from .models import AnalysisError, decimal_text, decimal_value, digest, precise_sum

OPERATORS = {"SORT", "RANK", "COUNT", "SUM", "DIFFERENCE", "RATIO", "LINEAR", "CONVERT", "FORMULA_CHECK", "RECONCILE", "ARITHMETIC", "DESCRIBE"}


class RuleRegistry:
    def __init__(self, rules: dict | None = None):
        self.rules = rules or {}
        for key, rule in self.rules.items():
            if not rule.get("version") or not rule.get("source") or not rule.get("operators") or not set(rule["operators"]) <= OPERATORS:
                raise AnalysisError("INVALID_RULE", "登记规则需要来源、版本及明确算子范围。")
            if 'comparability' in rule:
                from .comparability import validate_policy
                validate_policy(rule['comparability'])
        self.version = digest(self.rules)

    def resolve(self, ref, operator, facts):
        rule = self.rules.get(ref)
        if not rule or operator not in rule["operators"]:
            raise AnalysisError("RULE_REQUIRED", "该业务计算需要有来源且适用的已登记规则。")
        for field, expected in rule.get("appliesTo", {}).items():
            if any(_field(fact, field) != expected for fact in facts):
                raise AnalysisError("RULE_NOT_APPLICABLE", "已登记规则不适用于所选指标或范围。")
        return rule


def _field(value, field):
    for part in field.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _select(facts, operation, known):
    if operation.get("inputRefs"):
        try:
            return [known[ref] for ref in operation["inputRefs"]]
        except KeyError as exc:
            missing = [ref for ref in operation['inputRefs'] if ref not in known]
            raise AnalysisError("UNKNOWN_VALUE", "计算引用不存在或尚未计算的值。",
                                {'missingRefs': missing, 'nextAction': 'USE_EXISTING_VALUE_REFS',
                                 'hint': '使用真实 factId、derivedId 或前序运算 id；不要重新查询已有数据。'}) from exc
    selected = facts
    selector = operation.get("select", {})
    allowed = {"cellKey", "measureCode", "objectId", "bindingRef", "period", "sourceContextId", "dimensions",
               "batchId", "columnName", "inputRole", "sourceObjectId"}
    if set(selector) - allowed:
        raise AnalysisError("INVALID_SELECTOR", "计算选择器包含不支持的字段。")
    for key, expected in selector.items():
        if key == "dimensions":
            selected = [f for f in selected if all(f.get("dimensions", {}).get(k) == v for k, v in expected.items())]
        else:
            selected = [f for f in selected if f.get(key) == expected]
    return selected


def _numeric(items, allow_display=False):
    if not items:
        raise AnalysisError("EMPTY_INPUT", "没有符合条件的数值。")
    if any(x.get("valueState") != "PRESENT" or x.get("calculationValidation", "PASS") != "PASS" for x in items):
        raise AnalysisError("INCOMPLETE_VALUES", "计算包含缺值、失败、冲突或尚未查询的项。")
    if not allow_display and any(x.get("precision") == "display_only" for x in items):
        raise AnalysisError("PRECISION_UNPROVEN", "显示精度不能直接作为精确计算输入。")
    return [decimal_value(x["value"]) for x in items]


def _comparable(facts, scope, same_metric=True, varying_dimensions=(), policy=None):
    from .comparability import check
    return check(facts, scope, same_metric, varying_dimensions, policy)


def source_aggregation_rule(facts):
    """仅复用同一原生聚合回执的 SUM 语义；不同批次或明细层级不推定可加。"""
    if not facts or any(fact.get('calcType') != 'SUM'
                        or any(not fact.get(key) for key in ('batchId', 'tableDigest', 'columnName')) for fact in facts):
        return None
    if len({(fact['batchId'], fact.get('tableDigest'), fact.get('columnName')) for fact in facts}) != 1:
        return None
    aggregation = facts[0].get('aggregation') or {}
    grouping = aggregation.get('groupBy') or []
    if not grouping or any(fact.get('aggregation') != aggregation for fact in facts):
        return None
    dimensions = [item['dimCode'] + ('__' + item['field'] if item.get('field') else '') for item in grouping]
    if any(any(key not in fact.get('dimensions', {}) for key in dimensions) for fact in facts):
        return None
    if len({digest([fact['dimensions'][key] for key in dimensions]) for fact in facts}) != len(facts):
        return None
    return {'version': facts[0].get('tableDigest'), 'source': 'native_aggregation:' + facts[0]['batchId'],
            'additive': True, 'exclusiveMembers': True, 'varyingDimensions': dimensions}


def calculate(facts: list[dict], scope: dict, operations: list[dict], registry: RuleRegistry, relations=None, failure_cache=None) -> list[dict]:
    from .operation_contracts import validate_operation
    from .scalar_references import normalize_reference, reference_id, reference_key, scalar_record
    known = {f["factId"]: f for f in facts}
    results, names = [], set()
    input_basis = digest([facts, scope, registry.version]) if failure_cache is not None else None
    preceding_fingerprints = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise AnalysisError('INVALID_OPERATION_ARGUMENT', '每项运算必须是对象。')
        oid, operator = operation.get("id", ""), operation.get("operator")
        if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", oid) or oid in names:
            raise AnalysisError("INVALID_OPERATION_ID", "计算标识必须唯一且符合命名范围。")
        names.add(oid)
        result = {"derivedId": "derived_" + oid, "operator": operator, "scopeId": scope["scopeId"],
                  "inputRefs": [], "ruleRef": operation.get("ruleRef"), "value": None,
                  "calculationValidation": "BLOCKED", "rounding": None, "diagnostics": []}
        failure_key = digest([input_basis, operation, preceding_fingerprints]) if failure_cache is not None else None
        try:
            if failure_cache is not None and failure_key in failure_cache:
                previous = failure_cache[failure_key]
                raise AnalysisError(previous['code'], previous['message'],
                                    {**previous.get('details', {}), 'repeatedRequest': True,
                                     'hint': '同一冻结输入与参数已失败；复用具体诊断，修正本项，不重查来源。'})
            validate_operation(operation)
            if operator not in OPERATORS:
                raise AnalysisError("UNSUPPORTED_OPERATOR", "不支持该计算类型。")
            # Comparators are available for explicit operations, never silently
            # included in the subject population's default selection.
            pool = facts
            if scope.get('referenceMembers') and not operation.get('select', {}).get('objectId'):
                pool = [f for f in facts if f['objectId'] in scope['members']]
            # 同一计算计划可直接引用前序运算 id；保存结果统一为真实 derivedId。
            aliases = {ref.removeprefix('derived_'): ref for ref in known if ref.startswith('derived_')}
            aliases.update({ref: ref for ref in known})
            bindings = [normalize_reference(ref, aliases) for ref in operation.get('inputRefs', [])]
            resolved = {**operation, 'inputRefs': [reference_id(ref) for ref in bindings]}
            items = _select(pool, resolved, known)
            if operator == 'ARITHMETIC':
                items = [scalar_record(item, binding, known) for item, binding in zip(items, bindings)]
                result['inputBindings'] = bindings
                result['selectedCoverageRefs'] = sorted({ref for item in items for ref in item['selectedCoverageRefs']})
            result["inputRefs"] = list(dict.fromkeys(x.get("factId", x.get("derivedId")) for x in items))
            if bindings and len({reference_key(ref) for ref in bindings}) != len(bindings):
                raise AnalysisError("DUPLICATE_INPUT", "同一事实不能在一个计算中重复计入。")
            source_ids = sorted({ref for item in items for ref in item.get("sourceFactIds", [item.get("factId")]) if ref})
            source_facts = [known[ref] for ref in source_ids]
            result["sourceFactIds"] = source_ids
            comparison_policy = None
            if operation.get('comparabilityRef'):
                if operator not in {'RANK', 'SUM', 'DIFFERENCE', 'RATIO', 'LINEAR', 'CONVERT'}:
                    raise AnalysisError('INVALID_COMPARABILITY_OPERATOR', '该算子不使用业务可比规则。')
                comparison_rule = registry.resolve(operation['comparabilityRef'], operator, source_facts)
                comparison_policy = comparison_rule.get('comparability')
                if not comparison_policy:
                    raise AnalysisError('RULE_REQUIRED', '所引用规则未声明可比范围。')
                result.update(comparabilityRef=operation['comparabilityRef'],
                              comparabilityVersion=comparison_rule['version'], comparabilitySource=comparison_rule['source'])
            if operation.get("wholePopulation") and scope["coverage"] != "COMPLETE":
                raise AnalysisError("POPULATION_INCOMPLETE", "来源尚未证明该分析覆盖全部对象。")
            if operation.get('wholePopulation') and {f['objectId'] for f in source_facts} != set(scope['members']):
                raise AnalysisError('POPULATION_MISMATCH', 'wholePopulation 的输入必须恰好覆盖主体 members，不能包含比较对象或遗漏主体。')
            result["scopeMeaning"] = "whole_population" if operation.get("wholePopulation") else "selected_members"
            if operator == "FORMULA_CHECK":
                from .formulas import trace_formula
                if len(items) != 1 or "factId" not in items[0]:
                    raise AnalysisError("FORMULA_TARGET", "表内公式核对需要唯一的来源目标单元格。")
                trace = trace_formula(facts, items[0]["factId"])
                result["formulaTrace"] = trace
                result["sourceFactIds"] = trace["sourceFactIds"]
                result["applicability"] = trace["applicabilityStatus"]
                check_scope = operation.get('checkScope', 'LOCAL')
                if check_scope not in ('LOCAL', 'RECURSIVE'):
                    raise AnalysisError('INVALID_CHECK_SCOPE', '公式核对范围使用 LOCAL 或 RECURSIVE。')
                result.update(checkScope=check_scope, unit=items[0].get('unit'))
                check = trace['targetLocalCheck'] if check_scope == 'LOCAL' else trace
                result["sourceConsistency"] = check['status'] if check['status'] in ('MATCH', 'MISMATCH') else 'NOT_CHECKED'
                if check['status'] == 'BLOCKED':
                    if check.get('completionReason') == 'SOURCE_VALUE_MISSING':
                        # 核对已完成与数值可用是两个条件；保留 BLOCKED，后续算子不得取其金额。
                        result.update(executionStatus='COMPLETED', valueState='NO_VALUE', value=None,
                                      completionReason='SOURCE_VALUE_MISSING', diagnostics=[{
                                          'code': 'FORMULA_SOURCE_VALUE_MISSING',
                                          'message': '必要输入已读取，来源空值导致无法完整复算；可说明限制，无须重复取数。'}])
                        results.append(result)
                        known[result['derivedId']] = result
                        if failure_cache is not None:
                            preceding_fingerprints.append([result['derivedId'], digest(result)])
                        continue
                    recovery = trace["recovery"]
                    if check_scope == 'LOCAL' and check.get('diagnostic'):
                        recovery = {'targetFactRef': items[0]['factId'], 'nextAction': 'CHECK_LOCAL_INPUTS',
                                    'diagnostic': check['diagnostic'],
                                    'hint': '按本层具体断点处理；不能把未读数据、冲突或显示精度不足当作真实空值。'}
                    raise AnalysisError("FORMULA_INCOMPLETE", "表内公式核对受阻，请按具体断点处理。", recovery)
                result.update(value=check['value'], meaning='captured_local_formula_arithmetic' if check_scope == 'LOCAL' else trace['meaning'])
            elif operator == 'DESCRIBE':
                from .native_analysis import describe
                result.update(describe(items, scope, operation))
            elif operator == "ARITHMETIC":
                from numeric_tools import calculate as arithmetic
                _numeric(items)
                output = arithmetic(operation.get('expression'), {f'v{i}': item['value'] for i, item in enumerate(items)}, operation.get('decimalPlaces'))
                result.update(value=output['value'], unit=None, meaning='explicit_arithmetic_not_business_rule',
                              expression=operation['expression'], rounding=output)
            elif operator == "RECONCILE":
                from .relations import reconcile
                check = reconcile(facts, scope, relations or [], operation, items)
                result.update(reconciliation=check, value=check['difference'], unit=items[0].get('unit'),
                              meaning='parent_minus_direct_children_sum', sourceConsistency=check['status'])
            elif operator in ("SORT", "RANK", "COUNT"):
                if any("factId" not in x for x in items):
                    raise AnalysisError("FACTS_REQUIRED", "该操作需要逐对象的来源事实。")
                if len({x["objectId"] for x in items}) != len(items):
                    raise AnalysisError("DUPLICATE_MEMBER", "每个对象只能选择一个可比较指标，不能重复计数。")
                valid = [x for x in items if x.get("valueState") == "PRESENT"]
                values = _numeric(valid, allow_display=operator != "RANK") if valid else []
                if operator == "RANK":
                    _comparable(items, scope, policy=comparison_policy)
                    _numeric(items)
                if operator == "COUNT":
                    result["counts"] = {"positive": sum(x > 0 for x in values), "negative": sum(x < 0 for x in values),
                                        "zero": sum(x == 0 for x in values), "unavailable": len(items) - len(valid), "observed": len(valid)}
                    result["value"] = str(len(valid))
                    result["unit"] = "项"
                else:
                    direction = operation.get("direction", "desc")
                    if direction not in ("asc", "desc"):
                        raise AnalysisError("INVALID_DIRECTION", "排序方向必须为 asc 或 desc。")
                    by_id = sorted(zip(valid, values), key=lambda pair: (pair[0]["objectId"], pair[0]["factId"]))
                    ordered = sorted(by_id, key=lambda pair: pair[1], reverse=direction == "desc")
                    result["orderedFactIds"] = [x[0]["factId"] for x in ordered]
                    result["unavailableFactIds"] = [x["factId"] for x in items if x not in valid]
                    result["meaning"] = "source_value_order" if operator == "SORT" else "comparable_metric_ranking"
                    result["direction"] = direction
            else:
                values = _numeric(items)
                rule = None
                if operator in ("SUM", "LINEAR", "CONVERT"):
                    if operator == 'SUM' and not operation.get('ruleRef') and all('factId' in item for item in items):
                        rule = source_aggregation_rule(source_facts)
                    rule = rule or registry.resolve(operation.get("ruleRef"), operator, source_facts)
                    result["ruleVersion"] = rule["version"]
                    result["ruleSource"] = rule["source"]
                _comparable(source_facts, scope, same_metric=operator in ("SUM", "DIFFERENCE", "RATIO"),
                            varying_dimensions=(rule or {}).get("varyingDimensions", ()) if operator in ('SUM', 'LINEAR') else (),
                            policy=comparison_policy)
                if len({item.get("unit") for item in items}) != 1 or any(item.get("unit") != source_facts[0]["unit"] for item in items):
                    raise AnalysisError("OPERAND_UNIT_CONFLICT", "运算项的计量单位与来源不一致，请分别计算。")
                value = None
                if operator == "SUM":
                    if not rule.get("additive") or not rule.get("exclusiveMembers"):
                        raise AnalysisError("ADDITIVITY_UNCONFIRMED", "规则未确认指标可加总和对象互不重叠。")
                    if len({x["objectId"] for x in source_facts}) != len(source_facts):
                        raise AnalysisError("OVERLAPPING_MEMBERS", "存在重复对象或重叠指标。")
                    flattened = [ref for item in items for ref in item.get("sourceFactIds", [item.get("factId")])]
                    if len(flattened) != len(set(flattened)):
                        raise AnalysisError("OVERLAPPING_INPUTS", "派生运算项包含重叠的来源事实。")
                    value = precise_sum(values)
                elif operator == "DIFFERENCE":
                    if len(values) != 2:
                        raise AnalysisError("ARITY", "差额计算需要两个值。")
                    value = precise_sum([values[0], values[1].copy_negate()])
                elif operator == "RATIO":
                    if len(values) != 2 or values[1] == 0:
                        raise AnalysisError("ZERO_OR_MISSING_DENOMINATOR", "比率需要两个值且分母不能为零。")
                    places = operation.get("decimalPlaces", 8)
                    if type(places) is not int or not 0 <= places <= 16:
                        raise AnalysisError("INVALID_ROUNDING", "比率精度必须在零到十六位之间。")
                    with localcontext() as context:
                        context.prec = 512
                        value = (values[0] / values[1]).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
                    result["rounding"] = {"mode": "ROUND_HALF_EVEN", "decimalPlaces": places}
                    result["ratioMeaning"] = "signed_net_ratio" if any(decimal_value(f["value"]) < 0 for f in source_facts) else "ratio"
                elif operator == "LINEAR":
                    terms = rule.get("terms", [])
                    if len(terms) != len(items) or not terms:
                        raise AnalysisError("FORMULA_INCOMPLETE", "构成公式的必需项不齐。")
                    products = []
                    with localcontext() as context:
                        context.prec = 512
                        for item, number, term in zip(items, values, terms):
                            if not term.get("match") or any(_field(item, k) != v for k, v in term["match"].items()):
                                raise AnalysisError("FORMULA_INPUT_MISMATCH", "构成项不符合已登记公式。")
                            products.append(number * decimal_value(term["coefficient"]))
                    value = precise_sum(products)
                    result["meaning"] = "arithmetic_composition_not_business_cause"
                elif operator == "CONVERT":
                    if len(values) != 1 or source_facts[0]["unit"] != rule.get("fromUnit"):
                        raise AnalysisError("CONVERSION_UNCONFIRMED", "换算缺少适用原单位。")
                    with localcontext() as context:
                        context.prec = 512
                        value = values[0] * decimal_value(rule["factor"])
                    if not rule.get("toUnit"):
                        raise AnalysisError("CONVERSION_UNCONFIRMED", "换算缺少目标单位。")
                result["value"] = decimal_text(value)
                result["unit"] = rule["toUnit"] if operator == "CONVERT" else "比值" if operator == "RATIO" else source_facts[0]["unit"]
            result["calculationValidation"] = "PASS"
            result["executionStatus"] = "COMPLETED"
            result["valueState"] = "PRESENT" if result["value"] is not None else "NOT_APPLICABLE"
            result["precision"] = "derived_decimal"
        except AnalysisError as exc:
            result["diagnostics"] = [exc.as_dict()]
            if failure_cache is not None and exc.code in {
                    'INVALID_OPERATION_ARGUMENT', 'INVALID_OPERATION_INPUTS', 'INVALID_SCALAR_REFERENCE',
                    'ARITHMETIC_INPUTS', 'UNKNOWN_ARITHMETIC_VARIABLE', 'UNUSED_ARITHMETIC_INPUT',
                    'INVALID_EXPRESSION', 'DUPLICATE_INPUT', 'RULE_REQUIRED'}:
                failure_cache[failure_key] = exc.as_dict()
                while len(failure_cache) > 32:
                    failure_cache.pop(next(iter(failure_cache)))
            if operator == 'RANK' and exc.code == 'UNIT_UNCONFIRMED':
                result['diagnostics'][0]['details'] = {
                    'nextAction': 'CHOOSE_ORDER_MEANING',
                    'hint': '仅按来源数值从高到低展示时，将该运算显式改为 SORT 后重建；需要可比业务排名时先补齐来源单位/缩放。不能手抄顺序代替未完成的运算。',
                    'sourceSortOperation': {**deepcopy(operation), 'operator': 'SORT'},
                }
            result["executionStatus"] = "BLOCKED"
            result["valueState"] = "FAILED"
        results.append(result)
        known[result["derivedId"]] = result
        if failure_cache is not None:
            preceding_fingerprints.append([result['derivedId'], digest(result)])
    return results
