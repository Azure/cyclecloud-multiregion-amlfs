// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

const {themes} = require('prism-react-renderer');
const site = new URL(
  process.env.DOCUSAURUS_SITE_URL || 'https://azure.github.io/cyclecloud-multiregion-amlfs/',
);

/** @type {import('@docusaurus/types').Config} */
module.exports = {
  title: 'Azure CycleCloud Multi-region plugin for AMLFS',
  tagline: 'Deploy and operate multi-region Slurm data staging with Azure CycleCloud.',
  staticDirectories: [],
  url: site.origin,
  baseUrl: `${site.pathname.replace(/\/+$/, '')}/`,
  organizationName: 'Azure',
  projectName: 'cyclecloud-multiregion-amlfs',
  trailingSlash: true,
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  markdown: {
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],
  plugins: ['./plugins/third-party-notices.cjs'],
  presets: [
    [
      'classic',
      {
        docs: {
          path: '../docs',
          sidebarPath: './sidebars.js',
        },
        blog: false,
        theme: {customCss: './src/css/custom.css'},
      },
    ],
  ],
  themeConfig: {
    colorMode: {defaultMode: 'light', respectPrefersColorScheme: false},
    docs: {sidebar: {hideable: true}},
    navbar: {
      title: 'Azure CycleCloud Multi-region plugin for AMLFS',
      items: [
        {to: '/docs/overview', label: 'Documentation', position: 'left'},
        {
          href: 'https://learn.microsoft.com/en-us/azure/cyclecloud/overview-ccws?view=cyclecloud-8',
          label: 'Microsoft Learn',
          position: 'left',
        },
        {
          href: 'https://github.com/Azure/cyclecloud-multiregion-amlfs',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'light',
      links: [
        {
          title: 'Get started',
          items: [
            {label: 'Overview', to: '/docs/overview'},
            {label: 'Quick start guide', to: '/docs/installation'},
            {label: 'Run and monitor jobs', to: '/docs/usage'},
          ],
        },
        {
          title: 'Advanced guide',
          items: [
            {label: 'Import the cluster-init project', to: '/docs/project-import'},
            {label: 'Provision regional infrastructure', to: '/docs/provisioning'},
            {label: 'Generate cluster configuration', to: '/docs/configuration'},
            {label: 'Import cluster configuration', to: '/docs/import-configuration'},
            {label: 'Configure scheduler identity', to: '/docs/managed-identity'},
          ],
        },
        {
          title: 'Troubleshoot',
          items: [
            {label: 'Troubleshooting', to: '/docs/troubleshooting'},
            {
              label: 'Third-party notices',
              href: `pathname://${site.pathname.replace(/\/+$/, '')}/THIRD-PARTY-NOTICES.txt`,
              target: '_self',
            },
          ],
        },
      ],
      copyright: 'Azure CycleCloud Multi-region plugin for AMLFS project documentation. Code: MIT. Azure icons: Microsoft terms of use.',
    },
    prism: {
      theme: themes.github,
      darkTheme: themes.dracula,
      additionalLanguages: ['bash', 'ini', 'json', 'python'],
    },
    mermaid: {theme: {light: 'neutral', dark: 'dark'}},
  },
};
