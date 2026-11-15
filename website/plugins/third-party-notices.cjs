// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const embeddedReviews = require('./notices/embedded.cjs');

const NOTICE_FILE = 'THIRD-PARTY-NOTICES.txt';
const INVENTORY_FILE = 'third-party-notices.json';
const legalName = /^(?:licen[cs]es?|copying|copyright|notices?|third[-_ ]?party[-_ ]?(?:notices|licenses))(?:[._ -]|$)/i;
const normalize = (text) => text.replace(/\r\n/g, '\n');
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const portable = (filename) => filename.split(path.sep).join('/');

function packageRoot(resource) {
  const parts = path.resolve(resource).split(path.sep);
  const index = parts.lastIndexOf('node_modules');
  if (index < 0) return null;
  const count = parts[index + 1]?.startsWith('@') ? 3 : 2;
  const root = parts.slice(0, index + count).join(path.sep);
  if (!fs.existsSync(path.join(root, 'package.json'))) {
    throw new Error(`No package identity for bundled module: ${resource}`);
  }
  return root;
}

function bundledPackages(compilation, webpackPackage) {
  const packages = new Map();
  const seen = new Set();
  function add(resource) {
    const root = packageRoot(resource);
    if (!root) return;
    if (!packages.has(root)) packages.set(root, new Set());
    packages.get(root).add(resource);
  }
  function visit(module) {
    if (seen.has(module)) return;
    seen.add(module);
    // Concatenation hides individual resources from the top-level chunk graph.
    if (module.modules) {
      for (const child of module.modules) visit(child);
    } else {
      const resource = module.nameForCondition?.() || module.resource;
      if (resource) add(resource.split('?')[0]);
    }
  }
  for (const chunk of compilation.chunks) {
    for (const module of compilation.chunkGraph.getChunkModulesIterable(chunk)) visit(module);
    if (chunk.hasRuntime()) add(webpackPackage);
  }
  return packages;
}

function embeddedPackages(packages, siteDir) {
  const lock = JSON.parse(fs.readFileSync(path.join(siteDir, 'package-lock.json'), 'utf8')).packages;
  const installed = new Map();
  for (const [location, metadata] of Object.entries(lock)) {
    if (!location) continue;
    const root = path.join(siteDir, location);
    const name = metadata.name || location.split('node_modules/').pop();
    installed.set(`${name}@${metadata.version}`, root);
  }
  const embeddedIn = new Map();
  const inlineNotices = new Map();
  const mappedBundles = new Set(['mermaid', '@mermaid-js/parser', 'prism-react-renderer']);
  function include(id, owner) {
    const root = installed.get(id);
    assert(root, `Missing installed notice source for embedded ${id} in ${owner}; add an exact notice-only npm alias.`);
    const meta = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
    assert.equal(`${meta.name}@${meta.version}`, id, `Wrong notice source for ${id}`);
    if (!packages.has(root)) packages.set(root, new Set());
    if (!embeddedIn.has(id)) embeddedIn.set(id, new Set());
    embeddedIn.get(id).add(owner);
  }
  // Only inspect source maps belonging to modules in emitted chunks, not the
  // installed dependency tree. Their pnpm paths identify exact embedded versions.
  for (const [root, resources] of [...packages]) {
    const {name, version} = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
    const owner = `${name}@${version}`;
    const review = embeddedReviews[owner];
    if (Object.keys(embeddedReviews).some((id) => id.startsWith(`${name}@`))) {
      assert(review, `Review embedded dependencies after upgrading ${owner}`);
      if (review.sourceOnly) {
        assert([...resources].every((resource) => path.relative(root, resource).startsWith(`src${path.sep}`)),
          `Unexpected prebundle in ${owner}`);
      } else if (resources.has(path.join(root, review.file))) {
        assert.equal(createHash('sha256').update(fs.readFileSync(path.join(root, review.file))).digest('hex'),
          review.sha256, `Changed prebundle: review ${owner}`);
        for (const id of review.packages) include(id, owner);
      } else {
        throw new Error(`Unreviewed prebundle entrypoint in ${owner}`);
      }
    }
    for (const resource of resources) {
      if (!fs.existsSync(`${resource}.map`)) {
        assert(!mappedBundles.has(name), `Missing dependency source map for ${owner}: ${path.relative(root, resource)}`);
        continue;
      }
      const map = JSON.parse(fs.readFileSync(`${resource}.map`, 'utf8'));
      for (const [index, source] of (map.sources || []).entries()) {
        if (!source.includes('node_modules/')) continue;
        const match = source.match(/\.pnpm\/((?:@[^/+]+\+)?[^/@]+)@([^/]+)/);
        if (match) {
          include(`${match[1].replace('+', '/')}@${match[2].split(/[_()]/)[0]}`, owner);
        } else {
          // This upstream source map has no package version, but preserves the
          // complete Node.js POSIX path implementation's original MIT notice.
          assert.equal(owner, '@mermaid-js/parser@2.0.1', `Unreviewed source map dependency: ${owner}: ${source}`);
          assert.equal(source, 'webpack://LIB/node_modules/path-browserify/index.js');
          const text = normalize(map.sourcesContent[index].split("'use strict';")[0]);
          assert(text.includes('Copyright Joyent') && text.includes('THE SOFTWARE IS PROVIDED'), 'Missing embedded path license');
          inlineNotices.set(owner, [{file: `${portable(path.relative(root, resource))}.map: ${source}`, text}]);
        }
      }
    }
  }
  return {embeddedIn, inlineNotices};
}

function readPackage(root, resources, overrides = {}) {
  const {name, version, license, licenses} = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  assert(name && version, `Missing package identity: ${root}`);
  const id = `${name}@${version}`;
  const files = new Set();
  const directories = new Set([root]);
  for (const resource of resources) {
    let directory = path.dirname(resource);
    while (directory !== root && directory.startsWith(root + path.sep)) {
      directories.add(directory);
      directory = path.dirname(directory);
    }
    if (fs.existsSync(`${resource}.LICENSE.txt`)) files.add(`${resource}.LICENSE.txt`);
  }
  function addLegalFiles(directory, all = false) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      if (!all && !legalName.test(entry.name)) continue;
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory() && (all || /^(?:licen[cs]es?|notices?)$/i.test(entry.name))) addLegalFiles(filename, true);
      else if (entry.isFile() && !/\.(?:[cm]?jsx?|tsx?|map|json|png|svg)$/i.test(entry.name)) files.add(filename);
    }
  }
  for (const directory of directories) addLegalFiles(directory);
  const texts = [...files].sort(compare).map((filename) => ({
    file: portable(path.relative(root, filename)),
    text: normalize(fs.readFileSync(filename, 'utf8')),
  }));
  for (const extra of overrides[id] || []) {
    const text = normalize(fs.readFileSync(extra.installed ? path.join(root, extra.installed) : extra.file, 'utf8'));
    if (extra.sha256) assert.equal(createHash('sha256').update(text).digest('hex'), extra.sha256, `Changed reviewed notice for ${id}`);
    assert(!extra.section || text.includes(extra.section), `Missing reviewed license section for ${id}`);
    texts.push({file: extra.source, text: extra.section ? text.slice(text.indexOf(extra.section)) : text});
  }
  if (!texts.some(({file, text}) => /licen[cs]e|copying/i.test(file)
    && text.trim().length >= 100 && /\b(?:permission|redistribution|grant|terms|public domain)\b/i.test(text))) {
    throw new Error(`Missing complete license text for bundled ${id}; review the installed package, not just its SPDX metadata.`);
  }
  for (const {file, text} of texts) {
    assert(text.trim(), `Empty notice ${id}: ${file}`);
  }
  const comments = new Set();
  for (const resource of resources) {
    if (!/\.(?:[cm]?jsx?|tsx?|css)$/.test(resource)) continue;
    for (const [comment] of fs.readFileSync(resource, 'utf8').matchAll(/\/\*[\s\S]*?\*\//g)) {
      if (/(?:copyright\s+(?:\(c\)|\d)|@license|@preserve)/i.test(comment)) comments.add(normalize(comment));
    }
  }
  for (const text of comments) {
    texts.push({file: `Bundled source attribution ${createHash('sha256').update(text).digest('hex')}`, text});
  }
  return {
    name, version, license: license || licenses || null,
    source: `https://registry.npmjs.org/${name}/-/${name.split('/').pop()}-${version}.tgz`,
    modules: [...resources].map((resource) => portable(path.relative(root, resource))).sort(compare),
    files: texts.sort((a, b) => compare(a.file, b.file)),
  };
}

function collectNotices(packages, overrides, {embeddedIn = new Map(), inlineNotices = new Map()} = {}) {
  const records = new Map();
  const errors = [];
  for (const [root, resources] of packages) {
    try {
      const record = readPackage(root, resources, overrides);
      const id = `${record.name}@${record.version}`;
      record.embeddedIn = [...(embeddedIn.get(id) || [])].sort(compare);
      record.files.push(...(inlineNotices.get(id) || []));
      if (records.has(id)) {
        const previous = records.get(id);
        const files = new Map(previous.files.map((file) => [file.file, file.text]));
        for (const file of record.files) {
          if (files.has(file.file)) assert.equal(files.get(file.file), file.text, `Conflicting notices for ${id}: ${file.file}`);
          files.set(file.file, file.text);
        }
        record.files = [...files].map(([file, text]) => ({file, text})).sort((a, b) => compare(a.file, b.file));
        record.modules = [...new Set([...previous.modules, ...record.modules])].sort(compare);
      }
      records.set(id, record);
    } catch (error) {
      errors.push(error.message);
    }
  }
  if (errors.length) throw new Error(errors.sort(compare).join('\n'));
  return [...records.values()].sort((a, b) => compare(`${a.name}@${a.version}`, `${b.name}@${b.version}`));
}

function renderNotices(records) {
  return [
    'Third-party notices for the documentation website',
    '',
    'These components contribute code to the distributed browser JavaScript/CSS,',
    'including lazy-loaded chunks and the bundler runtime. Build-only dependencies',
    'are not included merely because they are installed. Original license and',
    'notice texts follow; the project license does not replace these terms.',
    '',
    ...records.flatMap(({name, version, license, source, embeddedIn = [], files}) => [
      '='.repeat(78), `${name}@${version}`, `Source: ${source}`,
      `Package license metadata: ${license ? typeof license === 'string' ? license : JSON.stringify(license) : '(not specified)'}`, '',
      ...(embeddedIn.length ? [`Also embedded in: ${embeddedIn.join(', ')}`, ''] : []),
      ...files.flatMap(({file, text}) => [`--- ${file} ---`, text, '']),
    ]),
  ].join('\n');
}

class ThirdPartyNoticesPlugin {
  constructor({baseUrl = '/', overrides = {}, siteDir} = {}) {
    this.baseUrl = baseUrl;
    this.overrides = overrides;
    this.siteDir = siteDir;
  }

  apply(compiler) {
    const {Compilation, sources: {RawSource}} = compiler.webpack;
    new compiler.webpack.BannerPlugin({
      banner: `Third-party licenses and notices: ${this.baseUrl}${NOTICE_FILE}`,
      test: /\.(?:js|css)$/,
      stage: Compilation.PROCESS_ASSETS_STAGE_SUMMARIZE,
    }).apply(compiler);
    compiler.hooks.thisCompilation.tap('ThirdPartyNotices', (compilation) => {
      compilation.hooks.processAssets.tap({
        name: 'ThirdPartyNotices',
        stage: Compilation.PROCESS_ASSETS_STAGE_REPORT,
      }, () => {
        assert.equal(require('webpack/package.json').version, compiler.webpack.version, 'Resolve notice source for the active webpack runtime');
        const packages = bundledPackages(compilation, require.resolve('webpack/package.json'));
        const embedded = this.siteDir ? embeddedPackages(packages, this.siteDir) : undefined;
        const records = collectNotices(packages, this.overrides, embedded);
        this.records = records;
        compilation.emitAsset(NOTICE_FILE, new RawSource(renderNotices(records)));
        compilation.emitAsset(INVENTORY_FILE, new RawSource(JSON.stringify(records, null, 2) + '\n'));
      });
    });
  }
}

function validateBuild({outDir, baseUrl, expectedRecords}) {
  const notices = fs.readFileSync(path.join(outDir, NOTICE_FILE), 'utf8');
  const inventory = fs.readFileSync(path.join(outDir, INVENTORY_FILE), 'utf8');
  const records = JSON.parse(inventory);
  assert(records.length, 'Empty third-party inventory');
  assert.equal(notices, renderNotices(records), 'Notices are truncated or inconsistent with the inventory');
  if (expectedRecords) assert.deepEqual(records, expectedRecords, 'Notices do not cover the compiled module graph');
  assert(!notices.includes(path.resolve(__dirname, '..')), 'Local build path leaked into notices');
  assert(!/\b[A-Za-z]:[\\/](?!\/)|file:\/\/|\/(?:Users|home|__w)\/|_authToken|gh[pousr]_[A-Za-z0-9]{20,}/.test(notices + inventory),
    'Private path or credential-like text in notices');
  let assets = 0;
  let pages = 0;
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (/\.(js|css|html)$/.test(entry.name)) {
        const content = fs.readFileSync(filename, 'utf8');
        if (entry.name.endsWith('.html')) {
          // The existing home route is an empty client-side redirect, not a page.
          if (filename === path.join(outDir, 'index.html') && content.includes('<div id="__docusaurus"></div>')) continue;
          assert(content.includes(`href="${baseUrl}${NOTICE_FILE}"`), `Missing notice link: ${filename}`);
          pages++;
        } else {
          assert(content.includes(`Third-party licenses and notices: ${baseUrl}${NOTICE_FILE}`), `Missing notice banner: ${filename}`);
          assets++;
        }
      }
    }
  }
  visit(outDir);
  assert(assets && pages, 'No browser assets or HTML pages validated');
  console.log(`Validated third-party notices: ${records.length} components, ${assets} JS/CSS assets, ${pages} HTML pages.`);
}

module.exports = function thirdPartyNotices(context) {
  const overrides = {
    'infima@0.2.0-alpha.45': [{
      file: path.join(__dirname, 'notices', 'infima-0.2.0-alpha.45.LICENSE'),
      source: 'https://github.com/facebookincubator/infima/blob/205b8aedc62496839991b75a8bc2922350766fe4/LICENSE',
      sha256: '52412d7bc7ce4157ea628bbaacb8829e0a9cb3c58f57f99176126bc8cf2bfc85',
    }],
    'isarray@0.0.1': [{installed: 'README.md', section: '## License', source: 'README.md#License'}],
    'fastdom@1.0.12': [{installed: 'README.md', section: '## License', source: 'README.md#License'}],
    'elkjs@0.12.1': [{
      file: path.join(__dirname, 'notices', 'elkjs-0.12.1-runtime.NOTICE.txt'),
      source: 'Embedded Java runtime NOTICE (elkjs 0.12.1; provenance and source availability below)',
      sha256: '8a7c5bd7ec1457c6a5a161be7595156cdbe7a2e148b1efd09066807e13756144',
    }],
  };
  const plugin = new ThirdPartyNoticesPlugin({baseUrl: context.siteConfig.baseUrl, siteDir: context.siteDir, overrides});
  return {
    name: 'third-party-notices',
    configureWebpack(config, isServer) {
      return !isServer && config.mode === 'production'
        ? {plugins: [plugin]}
        : {};
    },
    postBuild(props) {
      validateBuild({...props, expectedRecords: plugin.records});
    },
  };
};
module.exports.ThirdPartyNoticesPlugin = ThirdPartyNoticesPlugin;
module.exports.packageRoot = packageRoot;
module.exports.collectNotices = collectNotices;
module.exports.renderNotices = renderNotices;
module.exports.validateBuild = validateBuild;
module.exports.embeddedPackages = embeddedPackages;
