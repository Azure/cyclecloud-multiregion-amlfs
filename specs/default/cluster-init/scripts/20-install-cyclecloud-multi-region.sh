#!/bin/bash
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

set -euo pipefail
FILES=/opt/cyclecloud-multi-region/downloads
install -d -m 0755 "$FILES"
rm -f "$FILES/cyclecloud-multi-region.tar.gz" "$FILES/cyclecloud-multi-region.tar.gz.sha256"
jetpack download --project cyclecloud-multi-region cyclecloud-multi-region.tar.gz "$FILES"
jetpack download --project cyclecloud-multi-region cyclecloud-multi-region.tar.gz.sha256 "$FILES"
cd "$FILES"
sha256sum --check cyclecloud-multi-region.tar.gz.sha256
install -d -m 0755 /opt/cyclecloud-multi-region/payload
tar --extract --gzip --file cyclecloud-multi-region.tar.gz \
    --directory /opt/cyclecloud-multi-region/payload --no-same-owner
exec python3 /opt/cyclecloud-multi-region/payload/scripts/install.py --jetpack
