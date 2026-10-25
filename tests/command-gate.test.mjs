import test from 'node:test';
import assert from 'node:assert/strict';
import {createGate, isReadOnly} from '../extension/command-gate.js';

const later = (ms, value) => new Promise(resolve => setTimeout(() => resolve(value), ms));

test('classifies reads and writes', () => {
  assert.equal(isReadOnly({kind: 'rest', method: 'GET'}), true);
  assert.equal(isReadOnly({kind: 'microversion'}), true);
  assert.equal(isReadOnly({kind: 'rest', method: 'POST'}), false);
  assert.equal(isReadOnly({kind: 'native'}), false);
});

test('a read waits for a still-running command instead of failing', async () => {
  const run = createGate({waitMs: 1000});
  const slow = run({kind: 'native'}, () => later(30, 'write done'));
  const read = await run({kind: 'rest', method: 'GET'}, async () => 'read done');
  assert.equal(await slow, 'write done');
  assert.equal(read, 'read done');
});

test('a write is refused while an earlier command still runs', async () => {
  const run = createGate({waitMs: 1000});
  const slow = run({kind: 'rest', method: 'GET'}, () => later(30, 'ok'));
  const write = await run({kind: 'native'}, async () => 'must not run');
  assert.match(write.error, /still running/);
  await slow;
});

test('a read gives up after the wait limit and execution errors stay short', async () => {
  const run = createGate({waitMs: 10});
  const slow = run({kind: 'native'}, () => later(60, 'ok'));
  const read = await run({kind: 'state'}, async () => 'must not run');
  assert.match(read.error, /still running/);
  await slow;
  const failed = await run({kind: 'state'}, async () => { throw new Error('x'.repeat(900)); });
  assert.equal(failed.error.length, 500);
});
