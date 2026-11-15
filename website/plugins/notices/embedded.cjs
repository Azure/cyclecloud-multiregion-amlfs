// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

// These bundles have no usable dependency source maps. Review their published
// sources/build manifests when changing versions; hashes prevent stale reviews.
module.exports = {
  'roughjs@4.6.6': {
    file: 'bundled/rough.esm.js',
    sha256: 'e921535f216ebc93d422489d614da955b5286812220b3149fc70978f4cb5bd45',
    packages: ['hachure-fill@0.5.2', 'path-data-parser@0.1.0', 'points-on-curve@0.2.0', 'points-on-path@0.2.1'],
  },
  '@upsetjs/venn.js@2.0.0': {
    file: 'build/venn.esm.js',
    sha256: '47e73982c0cf8a4e692c57a06656fc928aeced63267d9a1e6f48122c4127da30',
    packages: ['fmin@0.0.4'],
  },
  'd3-array@3.2.5': {
    file: 'dist/d3-array.mjs',
    sha256: 'b219965735dbd3c8a63fdd32ad2823a8f34f421399568f1ff6ae8391ce28df41',
    packages: ['internmap@2.0.3'],
  },
  'd3-array@2.12.1': {sourceOnly: true},
  'elkjs@0.12.1': {
    file: 'lib/elk.bundled.js',
    sha256: 'b1032b7cfd2a57c54c6f0e8369d7665e19a579954f7cd20a6dae28f1912ea0d7',
    packages: ['browser-pack@6.1.0', 'web-worker@1.4.1'],
  },
};
