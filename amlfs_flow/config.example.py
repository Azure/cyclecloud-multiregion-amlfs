"""AMLFS burst-buffer configuration — EXAMPLE template.

Copy to config.py and customize for your cluster, OR let scripts/install.sh
render it from CLI args.
"""

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"

# ── PRIMARY AMLFS (same region as the Slurm scheduler) ──────────────────
AMLFS_PRIMARY_RESOURCE_GROUP = "rg-amlfs-primary"
AMLFS_PRIMARY_NAME           = "amlfs-primary"
AMLFS_PRIMARY_MOUNT          = "/lustre"

# ── SECONDARY AMLFS (remote region; reached via blob staging) ───────────
AMLFS_SECONDARY_RESOURCE_GROUP = "rg-amlfs-secondary"
AMLFS_SECONDARY_NAME           = "amlfs-secondary"
AMLFS_SECONDARY_MOUNT          = "/lustre"

# Blob path = filesystem-relative path (shared HSM container).
# Set to a per-side prefix only if PRIMARY and SECONDARY are linked to
# *different* containers.
BLOB_STAGE_PREFIX = ""

# Partitions
PRIMARY_PARTITION   = "hpc"   # local — staging is SKIPPED for jobs here
SECONDARY_PARTITION = "htc"   # remote — triggers stage-in/stage-out

# Per-job state, locks, continuation tokens (shared via /sched)
STATE_DIR = "/sched/slurm-amlfs-flow"

# ImportJob conflict resolution — how a hydrated blob reconciles with
# an existing file on the destination AMLFS.
#   "Skip"             — keep the existing file (default for stage-in:
#                        avoids overwriting working data on the compute
#                        side mid-job)
#   "OverwriteAlways"  — replace destination unconditionally (default
#                        for stage-out: we *want* the new results back
#                        on the primary)
#   "OverwriteIfDirty" — replace only if the existing file diverges
#   "Fail"             — abort the ImportJob on any conflict
STAGEIN_CONFLICT_MODE  = "Skip"
STAGEOUT_CONFLICT_MODE = "OverwriteAlways"

# Timeouts (seconds)
ARCHIVE_POLL_INTERVAL = 15
IMPORT_POLL_INTERVAL  = 15
LRO_TIMEOUT           = 10800
