"""Validate the single current analysis contract on writes and reads."""
from functools import lru_cache
from itertools import islice
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .models import AnalysisError, ENGINE_VERSION, SCHEMA_VERSION


@lru_cache(maxsize=1)
def validator():
    schema = json.loads(Path(__file__).with_name("artifact-v1.schema.json").read_text("utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_artifact(artifact):
    from .findings import FINDING_VERSION
    if (artifact.get("schemaVersion") != SCHEMA_VERSION or artifact.get("engineVersion") != ENGINE_VERSION
            or artifact.get("findingVersion") != FINDING_VERSION or artifact.get("presentationPolicy") != "explicit"):
        raise AnalysisError("UNSUPPORTED_VERSION", "仅支持当前分析协议，请重新构建分析。")
    errors = list(islice(validator().iter_errors(artifact), 9))
    if errors:
        # 不把整个事实、HTML 或来源报文塞进错误；保留可定位的字段及约束。
        issues = []
        for error in errors[:8]:
            parts = [str(part).replace('~', '~0').replace('/', '~1') for part in error.absolute_path]
            detail = {'path': '/' + '/'.join(parts), 'constraint': error.validator}
            if error.validator == 'required' and isinstance(error.instance, dict):
                detail['missingFields'] = [field for field in error.validator_value if field not in error.instance]
            elif error.validator in ('type', 'enum', 'maxItems', 'minItems', 'maxLength', 'minLength'):
                detail['expected'] = error.validator_value
            issues.append(detail)
        raise AnalysisError("INVALID_ARTIFACT", "分析结果的内部协议校验失败，具体字段见 issues；重复查询来源不能修复此错误。",
                            {'issues': issues, 'hasMore': len(errors) > 8, 'retryable': False,
                             'requiresSourceQuery': False, 'nextAction': 'CHECK_ARTIFACT_CONTRACT'})
