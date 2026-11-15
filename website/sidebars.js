// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

module.exports = {
  guides: [
    {
      type: 'category',
      label: 'Overview',
      collapsed: false,
      link: {type: 'doc', id: 'overview'},
      items: [
        {type: 'doc', id: 'components', label: 'Components and ownership'},
        {type: 'doc', id: 'data-paths', label: 'Blob namespace and path mapping'},
        {type: 'doc', id: 'staging', label: 'Routing and transfer coordination'},
      ],
    },
    {
      type: 'category',
      label: 'Quick start',
      collapsed: false,
      link: {type: 'doc', id: 'installation'},
      items: [
        {type: 'doc', id: 'quickstart/prepare', label: '1. Prepare and upload the plugin'},
        {type: 'doc', id: 'quickstart/infrastructure', label: '2. Provision storage and permissions'},
        {type: 'doc', id: 'quickstart/configure', label: '3. Configure and import the cluster'},
        {type: 'doc', id: 'quickstart/activate', label: '4. Start and verify staging'},
        {type: 'doc', id: 'usage', label: '5. Run and monitor jobs'},
      ],
    },
    {
      type: 'category',
      label: 'Advanced guide',
      collapsed: false,
      items: [
        {type: 'doc', id: 'project-import', label: 'Import the cluster-init project'},
        {type: 'doc', id: 'provisioning', label: 'Provision regional infrastructure'},
        {type: 'doc', id: 'managed-identity', label: 'Configure scheduler identity'},
        {type: 'doc', id: 'configuration', label: 'Generate cluster configuration'},
        {type: 'doc', id: 'import-configuration', label: 'Import cluster configuration'},
        {type: 'doc', id: 'manual-infrastructure', label: 'Provision infrastructure manually'},
        {type: 'doc', id: 'manual-installation', label: 'Install on an existing scheduler'},
        {type: 'doc', id: 'slurm-template', label: 'Edit a Slurm template manually'},
        {type: 'doc', id: 'cyclecloud-import', label: 'Import or update a cluster'},
      ],
    },
    {type: 'doc', id: 'troubleshooting', label: 'Troubleshoot'},
  ],
};
