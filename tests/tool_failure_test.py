"""Root-cause, recovery and correlation regression tests for tool failures."""
import json
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from agentscope.message import ToolCallBlock
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, Toolkit, ToolResponse
from agentscope.tool._failure import tool_failure, failure_text


class ToolFailureTest(TestCase):
    def setUp(self):
        self.call = ToolCallBlock(id='call-1', name='query', input='{"requestId":"req-1"}')

    def test_nested_group_keeps_timeout_cause_and_query_identity(self):
        error = ExceptionGroup('unhandled errors in a TaskGroup', [
            ExceptionGroup('inner', [httpx.ReadTimeout('private response')]),
        ])
        result = tool_failure(error, self.call, read_only=True)
        self.assertEqual(result['toolCallId'], 'call-1')
        self.assertEqual(result['requestId'], 'req-1')
        self.assertEqual(result['causes'][0]['type'], 'ReadTimeout')
        self.assertTrue(result['retryable'])
        self.assertEqual(result['recoveryAction'], 'RETRY')
        self.assertNotIn('private response', failure_text(result))

    def test_http_status_is_preserved_without_body_or_credentials(self):
        request = httpx.Request('GET', 'https://internal.invalid/?token=secret')
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError('private body password=secret', request=request, response=response)
        error.__cause__ = TimeoutError('an earlier transport attempt')
        result = tool_failure(ExceptionGroup('group', [error]), self.call, read_only=True)
        self.assertEqual(result['causes'][0]['httpStatus'], 403)
        self.assertEqual(result['recoveryAction'], 'CHECK_PERMISSION')
        self.assertNotIn('secret', failure_text(result))
        self.assertFalse(result['retryable'])

    def test_write_timeout_requires_execution_check(self):
        result = tool_failure(TimeoutError(), self.call, read_only=False)
        self.assertFalse(result['retryable'])
        self.assertEqual(result['recoveryAction'], 'CHECK_EXECUTION')

    def test_mcp_sdk_timeout_keeps_protocol_code(self):
        error = ExceptionGroup('TaskGroup', [McpError(ErrorData(code=408, message='Timed out while waiting for response'))])
        result = tool_failure(error, self.call, read_only=True)
        self.assertEqual(result['causes'][0]['mcpCode'], 408)
        self.assertEqual(result['recoveryAction'], 'RETRY')
        self.assertNotIn('httpStatus', result['causes'][0])

    def test_unknown_and_mixed_causes_are_not_invented_timeouts(self):
        error = ExceptionGroup('group', [TimeoutError(), ValueError('invalid value')])
        result = tool_failure(error, self.call, read_only=True)
        self.assertEqual(len(result['causes']), 2)
        self.assertEqual(result['recoveryAction'], 'CHECK_DIAGNOSTICS')
        self.assertFalse(result['retryable'])

    def test_chained_error_keeps_underlying_cause(self):
        error = RuntimeError('wrapper')
        error.__cause__ = TimeoutError('source timeout')
        result = tool_failure(error, self.call, read_only=True)
        self.assertEqual([cause['type'] for cause in result['causes']], ['RuntimeError', 'TimeoutError'])
        self.assertTrue(result['retryable'])

    def test_large_and_cyclic_errors_are_bounded(self):
        error = RuntimeError('token=hidden ' + 'x' * 1000)
        error.__cause__ = error
        result = tool_failure(error, self.call)
        self.assertEqual(len(result['causes']), 1)
        self.assertNotIn('hidden', failure_text(result))
        quoted = tool_failure(ValueError('{"token": "hidden value", "password": "hidden password"}'), self.call)
        self.assertNotIn('hidden', failure_text(quoted))
        result = tool_failure(ExceptionGroup('many', [ValueError(str(i)) for i in range(100)]), self.call)
        self.assertTrue(result['causesTruncated'])
        self.assertLessEqual(len(result['causes']), 32)


class ToolFailureIntegrationTest(IsolatedAsyncioTestCase):
    async def test_toolkit_error_capture_contains_structured_diagnostics(self):
        async def query(requestId: str):
            """Fail with a nested transport error."""
            raise ExceptionGroup('TaskGroup', [TimeoutError('read timeout')])

        toolkit = Toolkit(tools=[FunctionTool(query, is_read_only=True)])
        call = ToolCallBlock(id='integration-call', name='query', input='{"requestId":"integration-request"}')
        results = [item async for item in toolkit.call_tool(call, AgentState())]
        response = next(item for item in results if isinstance(item, ToolResponse))
        self.assertEqual(str(response.state), 'error')
        diagnostic = response.metadata['toolFailure']
        self.assertEqual(diagnostic['requestId'], 'integration-request')
        self.assertEqual(diagnostic['toolCallId'], call.id)
        self.assertEqual(json.loads(response.content[0].text.split('：', 1)[1]), diagnostic)
