# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

# Appended immediately before the reviewed Workspace install.sh's final exit.
set -euo pipefail
umask 077
multi_region_work=/var/lib/cyclecloud-multi-region-ci
install -d -m 0700 "$multi_region_work"
test ! -e "$multi_region_work/status"
printf 'running\n' > "$multi_region_work/status"
trap 'printf "failed\n" > /var/lib/cyclecloud-multi-region-ci/status' ERR
az deployment group show --resource-group "$resource_group" \
    --name "@WORKSPACE_DEPLOYMENT_NAME@" \
    --query properties.outputs.multiRegionBundle.value --output tsv \
    > "$multi_region_work/bundle.base64"
python3 - <<'MULTI_REGION_UNPACK'
import base64
import hashlib
import io
from pathlib import Path
import tarfile

work = Path("/var/lib/cyclecloud-multi-region-ci")
data = base64.b64decode((work / "bundle.base64").read_text().strip(), validate=True)
if hashlib.sha256(data).hexdigest() != "@BUNDLE_SHA256@":
    raise ValueError("Bootstrap payload checksum mismatch")
source = work / "source"
source.mkdir(mode=0o700)
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
    for member in archive.getmembers():
        path = Path(member.name)
        if not member.isfile() or path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe bootstrap archive member")
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            stream.write(archive.extractfile(member).read())
        target.chmod(member.mode & 0o700)
MULTI_REGION_UNPACK
timeout 10800 python3 "$multi_region_work/source/ci/four_region.py" vm-install \
    --settings "$multi_region_work/source/ci-settings.json" \
    --slurm-version "$SLURM_PROJ_VERSION" > "$multi_region_work/install.log" 2>&1
printf 'succeeded\n' > "$multi_region_work/status"
trap - ERR
