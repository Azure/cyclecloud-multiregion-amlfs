#!/bin/bash
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

# Disable only our managed Slurm setting; retain data, configuration and mounts.
set -euo pipefail
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
python3 "$HERE/install.py" --role scheduler "$@"
echo "Extension disabled. Reconfigure Slurm before removing its installed runtime."
echo "Configuration, shared state, Lustre mounts and hooks are deliberately retained."
