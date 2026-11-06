#!/bin/bash
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

# Prepare input/message.txt and output/ on home AMLFS as the submitter; see docs/usage.md.
# Run sequentially: sbatch --partition=multi-regionB examples/job-example.sh
# All regions in examples/regions.json mount their own AMLFS at /lustre.

#SBATCH --partition=multi-regionB
#SBATCH --nodes=1
#SBATCH --time=00:30:00
#SBATCH --job-name=cyclecloud-multi-region-demo
#SBATCH --output=%x-%j.out

#BB_LUA stage_in source=/lustre/data/multi-region-demo/input type=directory
#BB_LUA stage_out source=/lustre/data/multi-region-demo/output type=directory

set -euo pipefail

input=/lustre/data/multi-region-demo/input/message.txt
output="/lustre/data/multi-region-demo/output/${SLURM_JOB_ID:?Run with sbatch}"
test -s "$input"
mkdir -p "$output"
cp "$input" "$output/message.txt"
sha256sum "$output/message.txt" > "$output/message.sha256"
printf 'job=%s\npartition=%s\nhost=%s\nutc=%s\n' \
    "$SLURM_JOB_ID" "${SLURM_JOB_PARTITION:-unknown}" "$(hostname)" \
    "$(date -u +%FT%TZ)" > "$output/execution.txt"
cat "$output/execution.txt"
