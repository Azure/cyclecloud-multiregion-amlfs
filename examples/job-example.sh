#!/bin/bash
# Example Slurm job using AMLFS burst-buffer staging.
#
# Submit with:
#   sbatch examples/job-example.sh
#
# The #BB_LUA directives are parsed by /etc/slurm/burst_buffer.lua.

#SBATCH -p htc                # secondary partition
#SBATCH -N 1
#SBATCH -t 00:30:00
#SBATCH -J amlfs-bb-demo
#SBATCH -o %x-%j.out

#BB_LUA stage_in  source=/lustre/data/myproject/input  type=directory
#BB_LUA stage_out source=/lustre/data/myproject/output type=directory

set -euo pipefail

echo "Running on $(hostname) at $(date)"
ls -la /lustre/data/myproject/input
mkdir -p /lustre/data/myproject/output

# ── your real workload ──
# ./solver --in /lustre/data/myproject/input --out /lustre/data/myproject/output

echo "Done at $(date)"
