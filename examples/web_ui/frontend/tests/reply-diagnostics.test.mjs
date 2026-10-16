import test from 'node:test';
import assert from 'node:assert/strict';
import {modelDiagnostics,replyErrorKey} from '../src/lib/reply-diagnostics.ts';
const diagnostics={callId:'a'.repeat(32),phase:'stream',failureKind:'protocol',elapsedMs:100,attempt:1};
test('stream interruption, timeout and HTTP failures remain distinct',()=>{
  assert.equal(replyErrorKey({type:'connection',diagnostics}),'connection_stream');
  assert.equal(replyErrorKey({type:'connection',diagnostics:{...diagnostics,failureKind:'timeout'}}),'connection_timeout');
  assert.equal(replyErrorKey({type:'connection',diagnostics:{...diagnostics,phase:'request'}}),'connection_protocol');
  assert.equal(replyErrorKey({type:'invalid_request',diagnostics}),'invalid_request');
});
test('diagnostics expose only validated correlation data',()=>{
  assert.deepEqual(modelDiagnostics({diagnostics:{...diagnostics,request:'private'}}),diagnostics);
  assert.equal(modelDiagnostics({diagnostics:{...diagnostics,elapsedMs:NaN}}),null);
  assert.equal(modelDiagnostics({diagnostics:{...diagnostics,callId:'untrusted'}}),null);
  assert.equal(replyErrorKey({type:'connection'}),'connection');
});
