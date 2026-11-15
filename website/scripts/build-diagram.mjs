// Copyright (c) Microsoft Corporation.
// SPDX-License-Identifier: MIT

import {readFile, writeFile} from 'node:fs/promises';

const assets = new URL('../../docs/assets/', import.meta.url);
const files = {
  vm: 'virtual-machine.svg',
  network: 'virtual-network.svg',
  identity: 'managed-identity.svg',
  blob: 'blob-storage.svg',
  lustre: 'managed-lustre.png',
};
const icons = Object.fromEntries(await Promise.all(
  Object.entries(files).map(async ([name, file]) => {
    const type = file.endsWith('.png') ? 'image/png' : 'image/svg+xml';
    const bytes = await readFile(new URL(`azure-icons/${file}`, assets));
    return [name, `data:${type};base64,${bytes.toString('base64')}`];
  }),
));
const icon = (name, x, y, size) =>
  `<image href="${icons[name]}" x="${x}" y="${y}" width="${size}" height="${size}" preserveAspectRatio="xMidYMid meet"/>`;
const regions = [
  {x: 25, name: 'Region A', role: 'Home'},
  {x: 275, name: 'Region B', role: 'Remote'},
  {x: 525, name: 'Region C', role: 'Remote'},
  {x: 775, name: 'Region D', role: 'Remote'},
];
const stages = [
  {
    id: 'archive-input', label: '1 Archive input',
    detail: 'Stage-in: archive input from home AMLFS (Region A) to shared Blob.',
    path: 'M137 514V549H512V590',
  },
  {
    id: 'import-input', label: '2 Import input',
    detail: 'Stage-in: import input from shared Blob into Region B AMLFS.',
    path: 'M512 590V549H387V514',
  },
  {
    id: 'run-job', label: '3 Run job',
    detail: 'Compute in Region B reads input and writes output on its local /lustre.',
    path: 'M314 403V373', localIO: true,
  },
  {
    id: 'archive-output', label: '4 Archive output',
    detail: 'Stage-out: archive output from Region B AMLFS to shared Blob.',
    path: 'M387 514V549H512V590',
  },
  {
    id: 'return-output', label: '5 Return output',
    detail: 'Stage-out: import output from shared Blob back to home AMLFS (Region A).',
    path: 'M512 590V549H137V514',
  },
  {
    id: 'complete', label: '6 Complete',
    detail: 'Complete: output is available on Region A /lustre.',
  },
];
const stageX = (index) => 40 + index * 158;
const stageAnimation = (stage, index) => {
  const start = index / stages.length;
  const end = (index + 1) / stages.length;
  const times = [...new Set([0, start, end, 1])];
  const visibility = times.map(time => time >= start && time < end ? 1 : 0);
  const finalPoint = stage.localIO ? 0 : 1;
  const motion = new Map([
    [0, 0], [start, 0],
    ...(stage.localIO ? [[(start + end) / 2, 1]] : []),
    [end, finalPoint], [1, finalPoint],
  ]);
  return `
    <g id="stage-${stage.id}" class="stage-animation" aria-hidden="true" opacity="0">
      <animate attributeName="opacity" values="${visibility.join(';')}" keyTimes="${times.join(';')}" calcMode="discrete" dur="24s" repeatCount="indefinite"/>
      <rect x="${stageX(index)}" y="733" width="146" height="30" rx="4" fill="#f3edff" stroke="#7c3aed" stroke-width="2"/>
      <text x="${stageX(index) + 73}" y="753" text-anchor="middle" font-size="14" font-weight="600" fill="#7c3aed">${stage.label}</text>
      <rect x="40" y="774" width="945" height="26" fill="#fff"/>
      <text x="40" y="793" font-size="15" fill="#505050">${stage.detail}</text>
${stage.path ? `      <path d="${stage.path}" fill="none" stroke="#7c3aed" stroke-width="4" marker-end="url(#active-arrow)"/>
      <circle r="5" fill="#7c3aed" stroke="#fff" stroke-width="1.5">
        <animateMotion path="${stage.path}" dur="24s" repeatCount="indefinite" calcMode="linear" keyPoints="${[...motion.values()].join(';')}" keyTimes="${[...motion.keys()].join(';')}"/>
      </circle>` : ''}
    </g>`;
};

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1025" height="840" viewBox="0 0 1025 840" role="img" aria-labelledby="title description">
  <title id="title">Azure CycleCloud Multi-region plugin for AMLFS architecture</title>
  <desc id="description">Azure CycleCloud provisions regional compute VMs. Slurm schedules jobs, and the cyclecloud-multiregion-amlfs plugin coordinates Azure Managed Lustre transfers using the scheduler's managed identity for ARM requests. One example job runs in Region B. In order: archive input from home Region A to Blob; import input into Region B; run the job using local Lustre I/O; archive output from Region B to Blob; import output back into Region A; complete. Violet highlights the active transfer path. Each illustrative stage lasts four seconds. AMLFS HSM moves file bytes through Blob Storage. Reduced-motion mode shows the static architecture and staging sequence.</desc>
  <style>
    @media (prefers-reduced-motion: reduce) {
      .stage-animation { display: none; }
    }
  </style>
  <defs>
    <marker id="control-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 10 5 0 10" fill="none" stroke="#0078d4" stroke-width="1.5"/></marker>
    <marker id="data-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 10 5 0 10" fill="none" stroke="#107c10" stroke-width="1.5"/></marker>
    <marker id="active-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto"><path d="M0 0 10 5 0 10" fill="none" stroke="#7c3aed" stroke-width="1.5"/></marker>
  </defs>
  <rect width="1025" height="840" fill="#fff"/>
  <g font-family="Segoe UI, Arial, sans-serif" fill="#161616">
    <rect x="200" y="20" width="625" height="145" fill="#f5f5f5" stroke="#b8b8b8"/>
    <text x="220" y="46" font-size="15" font-weight="600">CONTROL PLANE</text>
    ${icon('vm', 225, 68, 48)}
    <text x="290" y="89" font-size="21" font-weight="600">Azure CycleCloud</text>
    <text x="290" y="117" font-size="16" fill="#505050">Compute provisioning / scaling</text>
    ${icon('vm', 555, 68, 48)}
    <text x="620" y="89" font-size="21" font-weight="600">Slurm scheduler</text>
    <text x="620" y="117" font-size="14" fill="#505050">Scheduling / plugin hooks</text>
    ${icon('identity', 557, 130, 20)}
    <text x="586" y="146" font-size="14" fill="#505050">Scheduler managed identity</text>
    <g fill="none" stroke="#0078d4" stroke-width="2">
      <path d="M512 165V194M137 194H887"/>
      <path d="M137 194V226" marker-end="url(#control-arrow)"/>
      <path d="M387 194V226" marker-end="url(#control-arrow)"/>
      <path d="M637 194V226" marker-end="url(#control-arrow)"/>
      <path d="M887 194V226" marker-end="url(#control-arrow)"/>
    </g>
    ${regions.map(({x, name, role}) => `
    <g>
      <rect x="${x}" y="235" width="225" height="270" fill="#f6fbff" stroke="#0078d4"/>
      ${icon('network', x + 12, 248, 26)}
      <text x="${x + 47}" y="268" font-size="19" font-weight="600">${name}</text>
      <text x="${x + 47}" y="292" font-size="14" fill="#505050">${role} / Azure VNet</text>
      ${icon('vm', x + 18, 321, 42)}
      <text x="${x + 76}" y="339" font-size="18" font-weight="600">Compute VMs</text>
      <text x="${x + 76}" y="363" font-size="15" fill="#505050">Slurm workload</text>
      <path d="M${x + 39} 373V403" stroke="#666" stroke-width="2"/>
      <text x="${x + 76}" y="394" font-size="14" fill="#505050">Local file I/O</text>
      ${icon('lustre', x + 18, 418, 42)}
      <text x="${x + 76}" y="431" font-size="16" font-weight="600">Azure Managed</text>
      <text x="${x + 76}" y="455" font-size="16" font-weight="600">Lustre</text>
      <text x="${x + 76}" y="482" font-family="monospace" font-size="15" fill="#505050">/lustre</text>
    </g>`).join('')}
    <g fill="none" stroke="#107c10" stroke-width="2.5">
      <path d="M137 514V549H887V514" marker-start="url(#data-arrow)" marker-end="url(#data-arrow)"/>
      <path d="M387 514V549" marker-start="url(#data-arrow)"/>
      <path d="M637 514V549" marker-start="url(#data-arrow)"/>
      <path d="M512 549V590" marker-end="url(#data-arrow)"/>
    </g>
    <rect x="307" y="599" width="410" height="76" fill="#f4faef" stroke="#107c10"/>
    ${icon('blob', 326, 614, 45)}
    <text x="391" y="630" font-size="19" font-weight="600">Shared Azure Blob container</text>
    <text x="391" y="655" font-size="16" fill="#505050">HSM archive / import namespace</text>
    <text x="28" y="631" font-size="14" fill="#0078d4">Blue: orchestration / ARM</text>
    <text x="28" y="657" font-size="14" fill="#107c10">Green: HSM data paths</text>
    <text x="28" y="680" font-size="14" fill="#7c3aed">Violet: active transfer</text>
    <rect x="25" y="691" width="975" height="116" rx="4" fill="#fff" stroke="#b8b8b8"/>
    <text x="40" y="715" font-size="16" font-weight="600">Example job: Region A (home) to Region B (remote)</text>
${stages.map((stage, index) => `
    <rect x="${stageX(index)}" y="733" width="146" height="30" rx="4" fill="#f5f5f5" stroke="#b8b8b8"/>
    <text x="${stageX(index) + 73}" y="753" text-anchor="middle" font-size="14" fill="#505050">${stage.label}</text>`).join('')}
    <text x="40" y="793" font-size="15" fill="#505050">Stage-in: A to Blob to B | Run in B | Stage-out: B to Blob to A</text>
${stages.map(stageAnimation).join('')}
    <text x="512" y="829" text-anchor="middle" font-size="14" fill="#505050">Illustrative timing. AMLFS HSM transfers data through Blob Storage.</text>
  </g>
</svg>
`;

await writeFile(new URL('architecture.svg', assets), svg);
console.log('Generated docs/assets/architecture.svg with unmodified Azure icons.');
