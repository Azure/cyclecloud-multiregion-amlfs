#!/bin/bash
# Verifies SDK connectivity and RBAC to both AMLFS instances.
set -e
exec /opt/slurm-amlfs-flow/venv/bin/python -m amlfs_flow selftest "$@"
