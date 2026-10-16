"""Validate the single current analysis contract on writes and reads."""
from functools import lru_cache
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
    if next(validator().iter_errors(artifact), None) is not None:
        raise AnalysisError("INVALID_ARTIFACT", "分析结果不符合已冻结的协议版本。")
