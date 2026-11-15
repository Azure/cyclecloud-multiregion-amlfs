// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import assert from 'node:assert/strict';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import handler from 'serve-handler';

test('preview serves pages and matches header globs with minimatch 11', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'docs-preview-'));
  const server = createServer((request, response) => {
    handler(request, response, {
      public: directory,
      cleanUrls: false,
      directoryListing: false,
      headers: [{source: '**/*.html', headers: [{key: 'X-Page-Match', value: 'yes'}]}],
    }).catch((error) => {
      response.statusCode = 500;
      response.end(error.message);
    });
  });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await rm(directory, {recursive: true});
  });
  await writeFile(join(directory, 'page.html'), '<h1>Preview</h1>');
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const page = await fetch(`${origin}/page.html`);
  assert.equal(page.status, 200);
  assert.equal(page.headers.get('x-page-match'), 'yes');
  assert.equal(await page.text(), '<h1>Preview</h1>');
  const missing = await fetch(`${origin}/missing.html`);
  assert.equal(missing.status, 404);
  await missing.text();
});
