// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import assert from 'node:assert/strict';
import test from 'node:test';
import deepmerge from 'deepmerge';

test('merge cannot poison the result prototype through target or null-prototype source', () => {
  const poisoned = JSON.parse('{"__proto__":{"admin":true},"nested":{"value":1}}');
  for (const result of [
    deepmerge(poisoned, {}),
    deepmerge(Object.create(null), poisoned),
    deepmerge.all([{}, {nested: poisoned}]).nested,
  ]) {
    assert.equal(Object.getPrototypeOf(result), Object.prototype);
    assert.equal(result.admin, undefined);
    assert.equal({}.admin, undefined);
  }
});

test('merge preserves ordinary objects, arrays, symbols and custom merges', () => {
  const key = Symbol('config');
  const result = deepmerge.all([
    {plugins: ['a'], nested: {a: 1}, [key]: {a: 1}},
    {plugins: ['b'], nested: {b: 2}, [key]: {b: 2}},
  ]);
  assert.deepEqual(result, {
    plugins: ['a', 'b'], nested: {a: 1, b: 2}, [key]: {a: 1, b: 2},
  });
  assert.deepEqual(deepmerge({plugins: ['a']}, {plugins: ['b']}, {
    customMerge: (key) => key === 'plugins' ? (_, source) => source : undefined,
  }), {plugins: ['b']});
});
