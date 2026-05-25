#!/bin/bash
# Removes slurm-amlfs-flow from the scheduler.
# Does NOT remove /etc/slurm/slurm.conf lines — edit manually.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root"
    exit 1
fi

echo "==> Removing /opt/slurm-amlfs-flow"
rm -rf /opt/slurm-amlfs-flow

echo "==> Removing /etc/slurm/burst_buffer.{lua,conf}"
rm -f /etc/slurm/burst_buffer.lua /etc/slurm/burst_buffer.conf

echo "==> Removing /sched/slurm-amlfs-flow"
rm -rf /sched/slurm-amlfs-flow

echo ""
echo "Remember to remove the following from /etc/slurm/slurm.conf:"
echo "    BurstBufferType=burst_buffer/lua"
echo "    DebugFlags=BurstBuffer"
echo "Then run: scontrol reconfigure"
