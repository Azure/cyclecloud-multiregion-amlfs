<!-- Copyright (c) Microsoft Corporation. SPDX-License-Identifier: MIT -->

# Documentation-site third-party notices

`npm run build` emits `THIRD-PARTY-NOTICES.txt` and
`third-party-notices.json`. The footer links to the text file using Docusaurus's
`pathname://` escape so the project `baseUrl` is retained without adding a trailing
slash or invoking SPA routing. Every emitted JS/CSS chunk also has a notice banner.
Existing webpack license-comment extracts are retained.

## Coverage and provenance

The local plugin uses the **production client webpack chunk graph**, including
lazy chunks, concatenated modules, extracted CSS (`nameForCondition()`), and the
generated webpack runtime. Server-rendering dependencies and unused installed
packages are not automatically attributed as distributed code. Package roots are
resolved from each resource's actual nested `node_modules` location, not by resolving
every name from the website root. Multiple installed versions remain distinct.

For each component, the report preserves complete installed LICENSE/LICENCE,
COPYING, COPYRIGHT and NOTICE texts, additional legal documents in legal directories
and module ancestors, and copyright/license comments in the bundled source.
Only line endings are normalized. Package name, version, original license metadata,
public npm source URL, package-relative module paths and embedded provenance are
included. Sorting is independent of traversal order and local locale. No build-time
network requests, SPDX-derived license templates, or blanket source distributions
are used.

Source maps shipped by Mermaid, its parser, and prism-react-renderer identify
additional prebundled packages and exact versions. Those versions must have an
installed notice source; `notice-*` npm aliases supply only the missing versions.
The aliases are **not imported into browser code**. The lockfile locates these
already-identified components; it is not the distribution inventory.

`embedded.cjs` records the separately reviewed, hash-gated bundles without suitable
source maps:

* RoughJS 4.6.6: its published `bin` imports and bundle contain hachure-fill,
  path-data-parser, points-on-curve and points-on-path.
* @upsetjs/venn.js 2.0.0: the published source and package manifest identify the
  patched fmin 0.0.4 implementation.
* d3-array 3.2.5: its distributed module embeds InternMap; the old nested 2.12.1
  instance contributes source modules instead.
* elkjs 0.12.1: its Browserify prelude and embedded worker wrapper correspond to
  browser-pack 6.1.0 and web-worker 1.4.1 in the upstream release lockfile.

## Reviewed notice-source exceptions

There are **no exclusions for missing licenses**. Website maintainers own these
version-specific source exceptions and must re-review them on dependency upgrades:

* Infima 0.2.0-alpha.45 omits LICENSE from its npm package. The checked-in text is
  the unmodified LICENSE from its release commit
  `205b8aedc62496839991b75a8bc2922350766fe4`.
* isarray 0.0.1 and fastdom 1.0.12 publish their complete MIT notices in the
  `## License` section of their installed README. Those sections are retained.
* The parser's embedded `path-browserify` source does not identify a package
  version. Its source map preserves the complete original Joyent/Node.js MIT
  notice and states the Node.js v8.11.1 origin. That text is retained under the
  parser's versioned component; a package version is not invented.
* `elkjs-0.12.1-runtime.NOTICE.txt` preserves additional Java-runtime notices
  omitted from the npm package. Its provenance includes exact upstream license
  URLs, source archive URLs and SHA-256 digests. The five runtime component groups
  are GWT 2.13.0, Guava 31.1-jre, EMF Common/Ecore GWT 2.12.4, and Xtext Xbase
  2.36.0. These are identified by `build.gradle`, `ElkJs.gwt.xml`, and the class
  identities in the shipped `elk-worker.js`, not all Java build dependencies.
  Complete upstream license texts, source-header copyright notices, Xtext NOTICE
  and source-availability information are preserved. EMF's Maven POM declares
  EPL-1.0; its license is copied from upstream revision
  `875dfb39907ad0fd5a94d2c67ae5bc91d82ca1fe`. Original notices may describe a wider
  upstream distribution; they do not expand the website component inventory.

No license metadata is overridden, and the website notices do not replace the
repository's MIT license, Microsoft trademark policy, or Azure icon terms.

## Validation and maintenance

The Docusaurus `postBuild` hook checks the emitted notices against the collected
module graph, rejects truncation and local-path/credential-like text, verifies
banners in **every** JS/CSS asset, and checks footer links in every rendered HTML
page (except the existing empty home redirect). `npm test` exercises real optimized
webpack builds with nested versions, concatenation, React's complete MIT notice,
extracted CSS, unused dependencies, missing licenses, additional NOTICE files,
determinism, source-map aliases, bad links and truncated output.

When adding/upgrading a prebundled dependency, review its maps and published build
inputs. Do not paper over failures with generic license text or exclusions. Extend
the versioned/hash-gated review for non-module imports or new opaque prebundles.
Missing required maps, exact embedded versions or license text fail the build.

API references:

* https://docusaurus.io/docs/api/plugin-methods/lifecycle-apis
* https://docusaurus.io/docs/advanced/routing#escaping-from-spa-redirects
* https://webpack.js.org/api/compilation-object/
* https://webpack.js.org/api/module-object/
* https://webpack.js.org/api/compilation-hooks/#processassets
* https://webpack.js.org/plugins/banner-plugin/
* https://github.com/kieler/elkjs/tree/0.12.1
