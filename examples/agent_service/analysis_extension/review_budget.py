"""Configurable character-based token estimate for independent review calls.

The deployment profile is explicit; it does not infer a provider's capacity
from the main Agent's compression threshold or download a tokenizer.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import math
import os

from .models import AnalysisError


@dataclass(frozen=True)
class ReviewBudgetConfig:
    # Current review deployment profile. Configure this when changing models.
    context_window_tokens: int = 1048576
    tokens_per_char: float = 1.0
    output_tokens: int = 65536
    safety_tokens: int = 8192
    call_timeout_seconds: int = 600
    final_reserve_seconds: int = 600
    reviewer_identity: str = ''
    tool_result_target_tokens: int = 12000
    active_material_target_tokens: int = 48000
    calculation_target_tokens: int = 128000
    final_target_tokens: int = 64000
    minimum_retry_seconds: int = 45

    def __post_init__(self):
        for key in ('context_window_tokens', 'output_tokens', 'call_timeout_seconds', 'final_reserve_seconds',
                    'tool_result_target_tokens', 'active_material_target_tokens', 'calculation_target_tokens',
                    'final_target_tokens', 'minimum_retry_seconds'):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise AnalysisError('REVIEW_CONFIG', f'{key} 必须是正整数。')
        if type(self.safety_tokens) is not int or self.safety_tokens < 0:
            raise AnalysisError('REVIEW_CONFIG', '审核安全余量必须为非负整数。')
        if (isinstance(self.tokens_per_char, bool) or not isinstance(self.tokens_per_char, (int, float))
                or not math.isfinite(self.tokens_per_char) or self.tokens_per_char <= 0):
            raise AnalysisError('REVIEW_CONFIG', '字符估算系数必须是有限正数。')
        if self.output_tokens + self.safety_tokens >= self.context_window_tokens:
            raise AnalysisError('REVIEW_CONFIG', '审核输出预留和余量不能占满上下文。')

    @classmethod
    def from_environment(cls, model=None):
        fields = {
            'context_window_tokens': ('ANALYSIS_REVIEW_CONTEXT_TOKENS', int),
            'tokens_per_char': ('ANALYSIS_REVIEW_TOKENS_PER_CHAR', float),
            'output_tokens': ('ANALYSIS_REVIEW_OUTPUT_TOKENS', int),
            'safety_tokens': ('ANALYSIS_REVIEW_SAFETY_TOKENS', int),
            'call_timeout_seconds': ('ANALYSIS_REVIEW_CALL_TIMEOUT_SECONDS', int),
            'final_reserve_seconds': ('ANALYSIS_REVIEW_FINAL_RESERVE_SECONDS', int),
            'tool_result_target_tokens': ('ANALYSIS_TOOL_RESULT_TARGET_TOKENS', int),
            'active_material_target_tokens': ('ANALYSIS_ACTIVE_MATERIAL_TARGET_TOKENS', int),
            'calculation_target_tokens': ('ANALYSIS_CALCULATION_TARGET_TOKENS', int),
            'final_target_tokens': ('ANALYSIS_FINAL_REVIEW_TARGET_TOKENS', int),
            'minimum_retry_seconds': ('ANALYSIS_REVIEW_MIN_RETRY_SECONDS', int),
        }
        try:
            values = {field: cast(os.environ[env]) for field, (env, cast) in fields.items() if env in os.environ}
        except ValueError as error:
            raise AnalysisError('REVIEW_CONFIG', '审核容量配置不是有效数字。') from error
        output = getattr(getattr(model, 'parameters', None), 'max_tokens', None)
        if output is not None and 'output_tokens' not in values:
            values['output_tokens'] = output
        if model is not None:
            from .models import digest
            params = getattr(model, 'parameters', None)
            # Only non-secret generation settings; credentials never enter logs or keys.
            settings = {key: getattr(params, key, None) for key in
                        ('max_tokens', 'temperature', 'top_p', 'seed', 'thinking', 'reasoning_effort')}
            values['reviewer_identity'] = digest([type(model).__module__, type(model).__name__,
                str(getattr(model, 'model', '')), {key: str(value) for key, value in settings.items()}])
        return cls(**values)

    def estimate(self, request, phase=None):
        chars = len(json.dumps(request, ensure_ascii=False, separators=(',', ':')))
        estimated = math.ceil(chars * self.tokens_per_char)
        required = estimated + self.output_tokens + self.safety_tokens
        result = {'inputChars': chars, 'estimatedInputTokens': estimated,
                'tokensPerChar': self.tokens_per_char, 'outputReserveTokens': self.output_tokens,
                'safetyTokens': self.safety_tokens, 'contextWindowTokens': self.context_window_tokens,
                'requiredTokens': required, 'fits': required <= self.context_window_tokens,
                'countMethod': 'CHARACTER_COEFFICIENT_ESTIMATE'}
        if phase:
            target = self.calculation_target_tokens if phase == 'CALCULATION' else self.final_target_tokens
            result.update(phase=phase, targetInputTokens=target, exceedsOptimizationTarget=estimated > target,
                          optimizationTargetIsCapacityLimit=False)
        return result

    def require(self, request, phase=None):
        result = self.estimate(request, phase)
        if not result['fits']:
            raise AnalysisError('REVIEW_CAPACITY', '完整审核请求超过配置的估算容量；已保留结果，未截断业务数据。',
                                {**result, 'requiresSourceQuery': False, 'nextAction': 'DELIVER_CURRENT_RESULTS'})
        return result

    def material_limit(self, prompt='', schema=None):
        """Conservative preparation allowance; actual full request is checked again."""
        fixed = len(json.dumps(review_request(prompt, '', schema), ensure_ascii=False, separators=(',', ':')))
        available = math.floor((self.context_window_tokens - self.output_tokens - self.safety_tokens)
                               / self.tokens_per_char) - fixed
        if available <= 0:
            raise AnalysisError('REVIEW_CAPACITY', '审核提示词与输出约定已超过配置容量，当前结果保留。')
        return available

    def identity(self):
        return asdict(self)

    def semantic_identity(self):
        # Transport tuning/soft targets do not invalidate a completed semantic review.
        return {'reviewerIdentity': self.reviewer_identity, 'outputTokens': self.output_tokens}

    def target_chars(self, tokens):
        return max(1, math.floor(tokens / self.tokens_per_char))


_CURRENT = ContextVar('analysis_review_budget', default=None)


def current_budget():
    return _CURRENT.get() or ReviewBudgetConfig.from_environment()


@contextmanager
def use_budget(config):
    token = _CURRENT.set(config)
    try:
        yield config
    finally:
        _CURRENT.reset(token)


def review_request(prompt, payload, schema=None):
    """All business messages and the actual structured-output schema, no credentials.

    SDK role markers and its short tool instruction are covered by safety_tokens.
    This remains an estimate, not a claim to reproduce provider tokenization.
    """
    return {'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': payload}],
            'tools': [{'type': 'function', 'function': {'name': 'generate_structured_output',
                       'parameters': schema or {}}}]}
