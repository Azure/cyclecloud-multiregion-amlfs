// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import webpack from 'webpack';
import MiniCssExtractPlugin from 'mini-css-extract-plugin';
import notices from '../plugins/third-party-notices.cjs';

const require = createRequire(import.meta.url);
const {ThirdPartyNoticesPlugin, collectNotices, renderNotices, embeddedPackages, validateBuild} = notices;
const license = fs.readFileSync(path.join(path.dirname(require.resolve('react/package.json')), 'LICENSE'), 'utf8');

function fixture(t) {
  const parent = path.resolve('.test-work');
  fs.mkdirSync(parent, {recursive: true});
  const root = fs.mkdtempSync(path.join(parent, 'notices-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  function write(filename, text) {
    const full = path.join(root, filename);
    fs.mkdirSync(path.dirname(full), {recursive: true});
    fs.writeFileSync(full, text);
    return full;
  }
  function pkg(location, name, version, source, withLicense = true) {
    write(path.join(location, 'package.json'), JSON.stringify({name, version, license: 'MIT', main: 'index.js', sideEffects: false}));
    if (withLicense) write(path.join(location, 'LICENSE'), license);
    return write(path.join(location, 'index.js'), source);
  }
  return {root, write, pkg};
}

async function build(root, baseUrl = '/project/') {
  const plugin = new ThirdPartyNoticesPlugin({baseUrl});
  const compiler = webpack({
    mode: 'production',
    context: root,
    entry: './entry.js',
    output: {path: path.join(root, 'build'), filename: '[name].[contenthash].js'},
    module: {rules: [{test: /\.css$/, use: [MiniCssExtractPlugin.loader, require.resolve('css-loader')]}]},
    plugins: [new MiniCssExtractPlugin(), plugin],
  });
  const stats = await new Promise((resolve, reject) => {
    compiler.run((error, result) => compiler.close((closeError) => {
      if (error || closeError) reject(error || closeError);
      else if (result.hasErrors()) reject(new Error(result.toString({all: false, errors: true})));
      else resolve(result);
    }));
  });
  return {stats, plugin, outDir: path.join(root, 'build'), baseUrl};
}

test('real optimized chunks cover nested versions, concatenated JS and extracted CSS, not unused dependencies', async (t) => {
  const {root, write, pkg} = fixture(t);
  pkg('node_modules/lib', 'lib', '1.0.0', 'export const value = Math.random();');
  pkg('node_modules/parent', 'parent', '1.0.0', 'export {value} from "lib";');
  pkg('node_modules/parent/node_modules/lib', 'lib', '2.0.0', 'export const value = Math.random() + 1;');
  write('node_modules/parent/node_modules/lib/NOTICE', 'Additional attribution must survive unchanged.\n');
  pkg('node_modules/unused', 'unused', '1.0.0', 'export const unused = 0;', false);
  pkg('node_modules/styles', 'styles', '1.0.0', '');
  write('node_modules/styles/package.json', JSON.stringify({name: 'styles', version: '1.0.0', license: 'MIT'}));
  write('node_modules/styles/index.css', 'body { color: red; }');
  write('entry.js', 'import React from "react"; import {value} from "lib"; import {value as nested} from "parent"; import "styles/index.css"; console.log(React.version, value, nested);');
  const result = await build(root);
  const records = JSON.parse(fs.readFileSync(path.join(result.outDir, 'third-party-notices.json')));
  const ids = records.map(({name, version}) => `${name}@${version}`);
  assert(ids.includes('lib@1.0.0') && ids.includes('lib@2.0.0'), ids.join(', '));
  assert(ids.includes('styles@1.0.0'));
  assert(!ids.some((id) => id.startsWith('unused@') || id.startsWith('css-loader@')));
  const nested = records.find((r) => r.name === 'lib' && r.version === '2.0.0');
  assert.equal(nested.files.find((f) => f.file === 'NOTICE').text, 'Additional attribution must survive unchanged.\n');
  assert.equal(nested.files.find((f) => f.file === 'LICENSE').text, license.replace(/\r\n/g, '\n'));
  const react = records.find((r) => r.name === 'react');
  assert.equal(react.files.find((f) => f.file === 'LICENSE').text, license.replace(/\r\n/g, '\n'));
  assert.match(react.files.find((f) => f.file === 'LICENSE').text, /Copyright \(c\) Meta Platforms, Inc\. and affiliates\./);
  write('build/index.html', '<a href="/project/THIRD-PARTY-NOTICES.txt">Third-party notices</a>');
  validateBuild({...result, expectedRecords: result.plugin.records});
  assert.throws(() => validateBuild({...result, baseUrl: '/'}), /Missing notice/);
  const text = fs.readFileSync(path.join(result.outDir, 'THIRD-PARTY-NOTICES.txt'), 'utf8');
  assert(!text.includes(root));
  write('build/THIRD-PARTY-NOTICES.txt', text.slice(0, -30));
  assert.throws(() => validateBuild(result), /truncated/);
});

test('an actually bundled package with only license metadata fails the build', async (t) => {
  const {root, write, pkg} = fixture(t);
  pkg('node_modules/missing', 'missing', '1.0.0', 'console.log("missing notice");', false);
  write('entry.js', 'import "missing";');
  // Keep the side-effect import in the bundle despite fixture package metadata.
  write('node_modules/missing/package.json', JSON.stringify({name: 'missing', version: '1.0.0', license: 'MIT'}));
  await assert.rejects(build(root), /Missing complete license text for bundled missing@1.0.0/);
  const file = write('node_modules/missing/LICENSE', 'MIT\n');
  assert.throws(() => collectNotices(new Map([[path.dirname(file), new Set()]])), /Missing complete license text/);
});

test('notice output is deterministic and preserves all license and NOTICE files', (t) => {
  const {pkg, write} = fixture(t);
  const a = pkg('node_modules/a', 'a', '1.0.0', '');
  const b = pkg('node_modules/b', 'b', '1.0.0', '');
  write('node_modules/a/LICENSE-extra', 'Additional license text.\r\n');
  write('node_modules/a/NOTICE', 'Additional attribution.\r\n');
  write('node_modules/a/licenses/embedded/NOTICE.txt', 'Nested upstream attribution.\r\n');
  write('node_modules/a/license-update.mjs', 'throw new Error("build-only source is not notice text");');
  write('node_modules/a/Copyright/index.js', 'export default "a UI component, not a license";');
  const entries = [[path.dirname(a), new Set([a])], [path.dirname(b), new Set([b])]];
  const records = collectNotices(new Map(entries));
  assert.equal(renderNotices(records), renderNotices(collectNotices(new Map(entries.reverse()))));
  assert.equal(records[0].files.find((f) => f.file === 'LICENSE-extra').text, 'Additional license text.\n');
  assert.equal(records[0].files.find((f) => f.file === 'NOTICE').text, 'Additional attribution.\n');
  assert.equal(records[0].files.find((f) => f.file === 'licenses/embedded/NOTICE.txt').text, 'Nested upstream attribution.\n');
  assert(!renderNotices(records).includes('build-only source'));
  assert(!renderNotices(records).includes('a UI component'));
});

test('prebundle source maps require exact installed versions, including npm aliases', (t) => {
  const {root, pkg, write} = fixture(t);
  const owner = pkg('node_modules/bundle', 'bundle', '1.0.0', '');
  const dependency = pkg('node_modules/notice-other', 'other', '2.0.0', '');
  write('node_modules/bundle/index.js.map', JSON.stringify({sources: ['../../node_modules/.pnpm/other@2.0.0/node_modules/other/index.js']}));
  write('package-lock.json', JSON.stringify({packages: {'node_modules/notice-other': {name: 'other', version: '1.0.0'}}}));
  assert.throws(() => embeddedPackages(new Map([[path.dirname(owner), new Set([owner])]]), root), /Missing installed notice source/);
  write('package-lock.json', JSON.stringify({packages: {'node_modules/notice-other': {name: 'other', version: '2.0.0'}}}));
  const packages = new Map([[path.dirname(owner), new Set([owner])]]);
  const embedded = embeddedPackages(packages, root);
  assert(packages.has(path.dirname(dependency)));
  assert.deepEqual(collectNotices(packages, {}, embedded).find((r) => r.name === 'other').embeddedIn, ['bundle@1.0.0']);
});

test('changed opaque bundles and missing dependency maps fail rather than losing embedded notices', (t) => {
  const {root, pkg, write} = fixture(t);
  pkg('node_modules/roughjs', 'roughjs', '4.6.6', '');
  const changed = write('node_modules/roughjs/bundled/rough.esm.js', 'console.log("changed upstream bundle");');
  write('package-lock.json', JSON.stringify({packages: {}}));
  assert.throws(() => embeddedPackages(new Map([[path.join(root, 'node_modules/roughjs'), new Set([changed])]]), root), /Changed prebundle/);
  const missingMap = pkg('node_modules/prism-react-renderer', 'prism-react-renderer', '2.4.1', '');
  assert.throws(() => embeddedPackages(new Map([[path.dirname(missingMap), new Set([missingMap])]]), root), /Missing dependency source map/);
});
