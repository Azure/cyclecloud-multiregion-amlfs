// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import {readFile, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const file = require.resolve('serve-handler');
const source = await readFile(file, 'utf8');
// serve-handler 6 expects minimatch's old callable CommonJS export.
const before = "const minimatch = require('minimatch');";
const after = "const {minimatch} = require('minimatch');";

if (source.split(before).length === 2) {
  await writeFile(file, source.replace(before, after));
} else if (!source.includes(after)) {
  throw new Error('serve-handler changed; review its minimatch compatibility patch.');
}
