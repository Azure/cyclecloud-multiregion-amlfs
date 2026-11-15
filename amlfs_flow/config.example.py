# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Azure CycleCloud Multi-region plugin for AMLFS configuration — EXAMPLE template.

Copy to config.py and supply real, provisioned AMLFS resources and RBAC.
Alternatively set AMLFS_FLOW_CONFIG to an absolute path to this Python config.
Every partition must explicitly map to a region. There is no region-count limit.
CLI: python -m amlfs_flow stage-in --job 123 --partition multi-regionB --paths /lustre/input
Stage-in paths use the home mountpoint; stage-out paths use the target mountpoint.
"""

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"

# Region keys are arbitrary. subscription_id is optional
# per region and falls back to SUBSCRIPTION_ID. Replace location placeholders
# with real Azure location identifiers; regionA-regionD are only logical labels.
REGIONS = {
    "regionA": {
        "resource_group": "rg-region-a",
        "filesystem": "amlfs-region-a",
        "location": "<region-a-azure-location>",
        "mountpoint": "/lustre",
    },
    "regionB": {
        "resource_group": "rg-region-b",
        "filesystem": "amlfs-region-b",
        "location": "<region-b-azure-location>",
        "mountpoint": "/lustre",
    },
    "regionC": {
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "resource_group": "rg-region-c",
        "filesystem": "amlfs-region-c",
        "location": "<region-c-azure-location>",
        "mountpoint": "/lustre",
    },
    "regionD": {
        "resource_group": "rg-region-d",
        "filesystem": "amlfs-region-d",
        "location": "<region-d-azure-location>",
        "mountpoint": "/lustre",
    },
}
HOME_REGION = "regionA"
PARTITION_REGIONS = {
    "hpc": "regionA",
    "htc": "regionA",
    "gpu": "regionA",
    "dynamic": "regionA",
    "multi-regionB": "regionB",
    "multi-regionC": "regionC",
    "multi-regionD": "regionD",
}

# Shared scheduler-owned state, locks and job routes. All callers must use the
# same directory on a filesystem supporting flock. Python serializes the whole
# archive/import stage by both Azure resource identities; Lua must not lock it.
# Teardown cleans only recorded job-owned exports/imports, never global archives.
# Routes are immutable for a job's lifetime: teardown before changing its route.
# Import completion is checked separately from ARM resource provisioning;
# partial/failed imports fail staging and retain cleanup state.
STATE_DIR = "/sched/cyclecloud-multi-region"

# ImportJob conflict resolution — how a hydrated blob reconciles with
# an existing file on the destination AMLFS.
#   "Skip"             — keep the existing file (default for stage-in:
#                        avoids overwriting working data on the compute
#                        side mid-job)
#   "OverwriteAlways"  — replace destination unconditionally (default
#                        for stage-out: we *want* the new results back
#                        on the home region)
#   "OverwriteIfDirty" — replace only if the existing file diverges
#   "Fail"             — abort the ImportJob on any conflict
STAGEIN_CONFLICT_MODE  = "Skip"
STAGEOUT_CONFLICT_MODE = "OverwriteAlways"

# Timeouts (seconds)
ARCHIVE_POLL_INTERVAL = 15
IMPORT_POLL_INTERVAL  = 15
LRO_TIMEOUT           = 10800
