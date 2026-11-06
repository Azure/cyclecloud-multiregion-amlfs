#!/bin/bash
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

set -euo pipefail
PYTHON=/opt/cyclecloud-multi-region/venv/bin/python
if [[ ! -x "$PYTHON" ]]; then
    echo "Enable and configure the scheduler extension before running selftest." >&2
    exit 1
fi
exec "$PYTHON" -I -m amlfs_flow selftest "$@"
