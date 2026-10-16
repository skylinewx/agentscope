import assert from 'node:assert/strict';
import test from 'node:test';
import { selectChatModel } from '../src/lib/model-selection.ts';

const schema = {
  type: 'object',
  properties: {
    max_tokens: { anyOf: [{ type: 'integer', exclusiveMinimum: 0 }, { type: 'null' }], maximum: 384000 },
    thinking_enable: { type: 'boolean', default: false },
    reasoning_effort: { anyOf: [{ type: 'string', enum: ['high', 'max'] }, { type: 'null' }] },
    temperature: { type: 'number', minimum: 0, maximum: 2 },
  },
};
const cards = ['fast', 'large'].map((name) => ({ name, parameter_schema: structuredClone(schema) }));
const original = {
  type: 'provider', credential_id: 'credential', model: 'fast',
  parameters: { max_tokens: 32768, thinking_enable: true, reasoning_effort: 'high' },
};
const choice = (model, overrides = {}) => ({ type: original.type, credential_id: original.credential_id, model, ...overrides });

test('reselecting the active model is a no-op even while the catalogue is loading', () => {
  assert.equal(selectChatModel(original, choice('fast'), []), original);
});

test('switching between models with shared parameter contracts preserves explicit settings', () => {
  const large = selectChatModel(original, choice('large'), cards);
  const roundTrip = selectChatModel(large, choice('fast'), cards);
  assert.deepEqual(large.parameters, original.parameters);
  assert.deepEqual(roundTrip, original);
  assert.deepEqual(JSON.parse(JSON.stringify({ chat_model_config: large })).chat_model_config.parameters, original.parameters);
  assert.notEqual(large.parameters, original.parameters);
});

test('false, zero and null settings are not mistaken for missing values', () => {
  const current = { ...original, parameters: { thinking_enable: false, temperature: 0, max_tokens: null } };
  assert.deepEqual(selectChatModel(current, choice('large'), cards).parameters, current.parameters);
});

test('selecting a different credential for the same provider and model preserves settings', () => {
  const result = selectChatModel(original, choice('fast', { credential_id: 'other' }), []);
  assert.deepEqual(result.parameters, original.parameters);
  assert.equal(result.credential_id, 'other');
});

test('different providers and unavailable model contracts do not inherit incompatible parameters', () => {
  assert.deepEqual(selectChatModel(original, choice('large', { type: 'other' }), cards).parameters, {});
  assert.deepEqual(selectChatModel(original, choice('large'), []).parameters, {});
  assert.deepEqual(selectChatModel(null, choice('large'), cards).parameters, {});
});

test('changed enum or token limits and unrecognised fields are not copied', () => {
  const changed = structuredClone(cards);
  changed[1].parameter_schema.properties.max_tokens.maximum = 8192;
  changed[1].parameter_schema.properties.reasoning_effort = { type: 'string', enum: ['low'] };
  const current = { ...original, parameters: { ...original.parameters, unknown: 'ignored' } };
  assert.deepEqual(selectChatModel(current, choice('large'), changed).parameters, { thinking_enable: true });
  assert.deepEqual(original.parameters, { max_tokens: 32768, thinking_enable: true, reasoning_effort: 'high' });
});

test('matching reference text does not bypass different schema definitions', () => {
  const referenced = [
    { name: 'fast', parameter_schema: { properties: { effort: { $ref: '#/$defs/effort' } }, $defs: { effort: { enum: ['high'] } } } },
    { name: 'large', parameter_schema: { properties: { effort: { $ref: '#/$defs/effort' } }, $defs: { effort: { enum: ['low'] } } } },
  ];
  assert.deepEqual(selectChatModel({ ...original, parameters: { effort: 'high' } }, choice('large'), referenced).parameters, {});
});
