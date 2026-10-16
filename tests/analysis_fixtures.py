"""De-identified contract fixtures, not imported business truth."""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "agent_service"))
from analysis_extension.models import Owner
from analysis_extension.engine import RuleRegistry

OWNER = Owner("fixture-user", "fixture-agent", "fixture-session")
CONTEXT = {"contextId": "context-A", "sourceId": "source-A", "snapshotId": "snapshot-A", "asOf": "2026-01-01T00:00:00Z", "schemaHash": "schema-A", "permissionStatus": "NOT_INTEGRATED"}


def fixture(values=("100", "-1033.14", "-8300"), unit="元", page=True):
    members = [f"object-{i}" for i in range(len(values))]
    request = {"contractVersion": "2.0", "contextId": CONTEXT["contextId"], "querySessionId": "query-A", "requestId": "request-A", "mode": "execute",
               "plan": {"steps": [{"id": "binding", "operation": "RESOLVE_ENTITY", "resourceIris": ["binding-A"]}], "outputStepIds": []}}
    receipt = {"contractVersion": "2.0", "executionId": "execution-A", "querySessionId": "query-A", "context": deepcopy(CONTEXT), "requestFingerprint": "request-digest",
               "status": "SUCCEEDED", "data": {"stepResults": [{"stepId": "population", "rows": [{"id": x, "key": x, "label": f"合成成员 {i}"} for i, x in enumerate(members)], "exactness": "EXACT"}]},
               "evidence": [], "branches": [], "completeness": {"enumerationExhausted": True, "semanticCompleteness": "UNVERIFIED"}}
    selectors = []
    for i, (member, value) in enumerate(zip(members, values)):
        sid, oid = f"form-{i}", f"observation-{i}"
        request["plan"]["steps"].append({"id": sid, "operation": "FORM_VIEW_QUERY", "inputs": ["binding"], "formQuery": {"periodCode": "2026N0001", "dimensions": {"MD_ORG": member}}})
        request["plan"]["outputStepIds"].append(sid)
        runtime = {"context": {"baseContext": deepcopy(CONTEXT), "target": {"formBindingKey": "binding-A"}, "period": "2026N0001",
                   "effectiveDimensions": {"MD_ORG": member, "DATATIME": "2026N0001", "MD_STAGE": "FINAL", "MD_CURRENCY": "CNY"}},
                   "observation": {"observationId": oid, "contentDigest": f"digest-{i}", "coverage": "COMPLETE"}}
        cell = {"key": "2:1", "row": 2, "column": 1, "measureCode": "AMOUNT", "dimensions": {"DATATIME": "2026N0001", "ACCOUNT": "profit", "SCENE": "budget"},
                "rawValueJson": json.dumps(value), "displayValue": value, "unit": unit, "headers": ["净利润"], "formula": None, "issue": None}
        grid = {"observationId": oid, "startRow": 1, "endRowExclusive": 3, "totalRows": 3, "totalColumns": 2,
                "coverage": "COMPLETE", "cells": [cell], "nextCursor": None, "observationExhausted": True, "contentDigest": f"digest-{i}"}
        form = {"resultType": "FORM_RESULT", "status": "GRID_LOADED" if page else "VALUE_FOUND", "disposition": "SUCCEEDED", "runtime": runtime,
                "formName": "合成利润表", "taskName": "合成任务", "coverage": "COMPLETE", "readAt": "2026-01-01T00:00:01Z", "cells": [] if page else [cell], "page": grid if page else None}
        receipt["data"]["stepResults"].append({"stepId": sid, "operation": "FORM_VIEW_QUERY", "formResults": [form], "exactness": "EXACT"})
        receipt["evidence"].append({"evidenceId": "evidence-" + oid, "kind": "FORM_RUNTIME", "fact": deepcopy(runtime)})
        selectors.append({"captureRef": "replace", "stepId": sid, "cellKeys": ["2:1"]})
    scope = {"label": "合成数据的指定对象", "selectionReason": "由已穷尽的合成成员清单选择", "members": members, "memberDimension": "MD_ORG", "expectedDimensions": {"DATATIME": "2026N0001"},
             "population": {"captureRef": "replace", "stepId": "population", "memberPointer": "/id"}}
    capture = {"toolName": "mcp__fixture__execute_semantic_plan", "originalToolName": "execute_semantic_plan", "resourceId": "fixture-resource", "toolCallId": "fixture-call", "request": request,
               "sourceAdapter": {"id": "budget-v2", "version": "1", "options": {}},
               "capturedAt": "2026-01-01T00:00:01Z", "captureState": "success", "response": {"state": "success", "content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=False)}]}}
    return capture, selectors, scope


def saved_fixture(store, values=("100", "-1033.14", "-8300"), unit="元", page=True):
    capture, selectors, scope = fixture(values, unit, page)
    ref = store.capture(OWNER, capture)
    for selector in selectors:
        selector["captureRef"] = ref
    scope["population"]["captureRef"] = ref
    return ref, selectors, scope


def rules():
    return RuleRegistry({"fixture-additive": {"version": "1", "source": "Synthetic additive metric definition for regression only", "operators": ["SUM"],
                         "appliesTo": {"measureCode": "AMOUNT", "dimensions.ACCOUNT": "profit"}, "additive": True, "exclusiveMembers": True}})
