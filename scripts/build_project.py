#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Generate native project blobs from the single maintained runtime source."""

import argparse
import configparser
import gzip
import hashlib
import io
from pathlib import Path
import re
import shutil
import tarfile


def build(root, wheelhouse=None):
    files = [
        *sorted((root / "amlfs_flow").glob("*.py")),
        *sorted((root / "slurm").glob("burst_buffer.*")),
        *sorted((root / "scripts").glob("*.py")),
        *sorted((root / "scripts").glob("*.sh")),
        root / "requirements.txt",
        root / "LICENSE",
    ]
    entries = {
        path.relative_to(root).as_posix(): path
        for path in files if path.name != "config.py"
    }
    if wheelhouse:
        wheels = sorted(wheelhouse.glob("*.whl"))
        if not wheels:
            raise ValueError("Wheelhouse contains no wheels")
        entries.update({"wheelhouse/" + path.name: path for path in wheels})
    output = root / "blobs"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "cyclecloud-multi-region.tar.gz"
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as tar:
                for name, path in sorted(entries.items()):
                    data = path.read_bytes()
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o755 if name.startswith("scripts/") else 0o644
                    tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "cyclecloud-multi-region.tar.gz.sha256").write_text(
        digest + "  " + archive.name + "\n"
    )
    return archive


def release(root, output, tag=None, wheelhouse=None):
    metadata = configparser.ConfigParser()
    metadata.read(root / "project.ini")
    project = metadata["project"]
    name, version = project["name"], project["version"]
    if name != "cyclecloud-multi-region" or project["type"] != "application":
        raise ValueError("Expected the cyclecloud-multi-region application project")
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Release project version must be MAJOR.MINOR.PATCH")
    if tag and tag != version:
        raise ValueError("Release tag must match project.ini: " + version)
    blob_names = ["cyclecloud-multi-region.tar.gz", "cyclecloud-multi-region.tar.gz.sha256"]
    if [name.strip() for name in metadata.get("blobs", "Files").split(",")] != blob_names:
        raise ValueError("project.ini must declare the runtime payload and checksum blobs")
    prefix = name + "-" + version
    archive = output / (prefix + ".tar.gz")
    checksum = output / (archive.name + ".sha256")
    if any(path.exists() for path in (archive, checksum, *[output / name for name in blob_names])):
        raise ValueError("Release output already exists; choose an empty output directory")
    build(root, wheelhouse)
    blobs = [root / "blobs" / name for name in blob_names]
    files = [
        root / "project.ini", root / "LICENSE",
        root / "specs/default/cluster-init/scripts/20-install-cyclecloud-multi-region.sh",
        *blobs,
    ]
    output.mkdir(parents=True, exist_ok=True)
    with archive.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as tar:
                for path in sorted(files):
                    relative = path.relative_to(root).as_posix()
                    data = path.read_bytes()
                    info = tarfile.TarInfo(prefix + "/" + relative)
                    info.size = len(data)
                    info.mode = 0o755 if relative.endswith(".sh") else 0o644
                    tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(digest + "  " + archive.name + "\n")
    for path in blobs:
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("Missing or empty declared release blob: " + path.name)
        shutil.copyfile(path, output / path.name)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path,
                        help="Include pre-downloaded wheels for the target OS/Python")
    parser.add_argument("--release-dir", type=Path,
                        help="Also package a complete, versioned project and checksum in this directory")
    parser.add_argument("--tag", help="Require a release tag matching project.ini (MAJOR.MINOR.PATCH)")
    args = parser.parse_args()
    if args.tag and not args.release_dir:
        parser.error("--tag requires --release-dir")
    root = Path(__file__).resolve().parents[1]
    if args.release_dir:
        print(release(root, args.release_dir, args.tag, args.wheelhouse))
    else:
        print(build(root, args.wheelhouse))
