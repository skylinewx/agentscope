import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
import httpx
from utils import MockModel
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, FinishedReason
from agentscope.model._diagnostics import ModelCallDiagnostic
from agentscope.app._service._errors import _classify_error


class DiagnosticTests(IsolatedAsyncioTestCase):
    async def test_stream_failure_keeps_partial_output_and_locates_transport_phase(self):
        model = MockModel()
        error = httpx.RemoteProtocolError('secret response body')
        model.set_responses([[ChatResponse(content=[TextBlock(text='已收到')], is_last=False), error]])
        stream = await model(messages=[UserMsg('user','private prompt')])
        received=[]
        with patch('agentscope.model._diagnostics.logger.warning') as log:
            with self.assertRaises(httpx.RemoteProtocolError):
                async for chunk in stream: received.extend(chunk.content)
        self.assertEqual(received[0].text,'已收到')
        info=_classify_error(error)
        self.assertEqual(info.type,'connection')
        self.assertEqual(info.diagnostics['phase'],'stream')
        self.assertEqual(info.diagnostics['failureKind'],'protocol')
        self.assertEqual(info.diagnostics['receivedChunks'],1)
        self.assertNotIn('secret',str(log.call_args))
        self.assertNotIn('private',info.model_dump_json())

    async def test_request_timeout_and_wrapped_cause_share_diagnostic(self):
        model=MockModel()
        error=httpx.ReadTimeout('secret')
        model.set_responses([error])
        with self.assertRaises(httpx.ReadTimeout): await model(messages=[])
        outer=RuntimeError('wrapper'); outer.__cause__=error
        info=_classify_error(outer)
        self.assertEqual(info.diagnostics['phase'],'request')
        self.assertEqual(info.diagnostics['failureKind'],'timeout')
        self.assertGreaterEqual(info.diagnostics['elapsedMs'],0)

    async def test_cancellation_does_not_emit_failure(self):
        model=MockModel(); model.set_responses([asyncio.CancelledError()])
        with patch('agentscope.model._diagnostics.logger.warning') as log:
            result=await model(messages=[])
        self.assertEqual(result.finished_reason,FinishedReason.INTERRUPTED)
        log.assert_not_called()

    async def test_structured_failure_is_traced_without_raw_data(self):
        class StructuredFailure(MockModel):
            async def _call_api_with_structured_output(self,*args,**kwargs):
                raise ValueError('private generated data')
        with self.assertRaises(ValueError) as caught:
            await StructuredFailure().generate_structured_output(messages=[UserMsg('user','test')],structured_model={'type':'object'})
        result=_classify_error(caught.exception).diagnostics
        self.assertEqual(result['phase'],'structured_output')
        self.assertNotIn('private',json.dumps(result))

    async def test_http_status_is_distinct_from_network_and_request_id_is_sanitized(self):
        error=httpx.HTTPStatusError('private body',request=httpx.Request('POST','https://example.invalid'),response=httpx.Response(400))
        error.request_id='secret containing spaces'
        ModelCallDiagnostic().failed(error,'request',1)
        info=_classify_error(error)
        self.assertEqual(info.type,'invalid_request')
        self.assertEqual(info.diagnostics['httpStatus'],400)
        self.assertIsNone(info.diagnostics['upstreamRequestId'])
