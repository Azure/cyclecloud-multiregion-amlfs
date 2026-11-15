#!/bin/bash
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

set -euo pipefail
FILES=/opt/cyclecloud-multi-region/downloads
install -d -m 0755 "$FILES"
rm -f "$FILES/cyclecloud-multiregion-amlfs.tar.gz" "$FILES/cyclecloud-multiregion-amlfs.tar.gz.sha256"
jetpack download --project cyclecloud-multiregion-amlfs cyclecloud-multiregion-amlfs.tar.gz "$FILES"
jetpack download --project cyclecloud-multiregion-amlfs cyclecloud-multiregion-amlfs.tar.gz.sha256 "$FILES"
cd "$FILES"
sha256sum --check cyclecloud-multiregion-amlfs.tar.gz.sha256
install -d -m 0755 /opt/cyclecloud-multi-region/payload
tar --extract --gzip --file cyclecloud-multiregion-amlfs.tar.gz \
    --directory /opt/cyclecloud-multi-region/payload --no-same-owner
exec python3 /opt/cyclecloud-multi-region/payload/scripts/install.py --jetpack
