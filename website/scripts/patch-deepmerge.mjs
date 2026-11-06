// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import {readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const file = require.resolve('deepmerge');
const source = await readFile(file, 'utf8');
const before = 'var destination = {};';
// A null prototype prevents __proto__ assignments from invoking an inherited setter.
const after = 'var destination = Object.create(null);';
const returnBefore = '\treturn destination\n';
const returnAfter = '\treturn Object.defineProperties({}, Object.getOwnPropertyDescriptors(destination))\n';

if (source.split(before).length === 2 && source.split(returnBefore).length === 2) {
  await writeFile(file, source.replace(before, after).replace(returnBefore, returnAfter));
} else if (!(source.includes(after) && source.includes(returnAfter))) {
  throw new Error('deepmerge changed; review the GHSA-gcqx-w48m-w2h4 patch.');
}
