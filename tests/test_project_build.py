# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

import hashlib
import io
import shutil
import tarfile

import pytest

from tests.test_project_install import ROOT, load_script, workspace


def test_payload_deterministic_and_canonical(workspace):
    builder = load_script("build_project")
    for directory in ("amlfs_flow", "slurm", "scripts", "ci"):
        (workspace / directory).mkdir()
    canonical = (ROOT / "amlfs_flow/__main__.py").read_bytes()
    (workspace / "amlfs_flow/__main__.py").write_bytes(canonical)
    (workspace / "amlfs_flow/config.py").write_text("SECRET = 'never package'\n")
    for name in ("ci/four_region.py", "ci/bootstrap.sh", "scripts/install.py"):
        (workspace / name).write_text("fixture\n")
    (workspace / "requirements.txt").write_text("azure-core>=1.30.0\n")
    (workspace / "LICENSE").write_text("license\n")
    wheels = workspace / "wheels"
    wheels.mkdir()
    (wheels / "example.whl").write_bytes(b"wheel")
    output = builder.build(workspace, wheels)
    assert output.parent == workspace / "blobs"
    assert output.name == "cyclecloud-multiregion-amlfs.tar.gz"
    assert not (workspace / "specs").exists()
    first = output.read_bytes()
    assert builder.build(workspace, wheels).read_bytes() == first
    with tarfile.open(output) as archive:
        assert archive.extractfile("amlfs_flow/__main__.py").read() == canonical
        assert "amlfs_flow/config.py" not in archive.getnames()
        assert not any(name.startswith("ci/") for name in archive.getnames())
        assert archive.extractfile("scripts/install.py").read() == b"fixture\n"
        assert archive.extractfile("wheelhouse/example.whl").read() == b"wheel"
        assert all(member.mtime == 0 and member.uid == 0 for member in archive.getmembers())
    assert output.with_suffix(".gz.sha256").read_text().startswith(hashlib.sha256(first).hexdigest())


def test_empty_offline_wheelhouse_rejected(workspace):
    builder = load_script("build_project")
    with pytest.raises(ValueError, match="no wheels"):
        builder.build(workspace, workspace)


def test_release_is_complete_reproducible_and_excludes_local_configuration(workspace):
    builder = load_script("build_project")
    for directory in ("amlfs_flow", "slurm", "scripts", "ci"):
        shutil.copytree(ROOT / directory, workspace / directory, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("project.ini", "LICENSE", "requirements.txt"):
        shutil.copyfile(ROOT / name, workspace / name)
    script = "specs/default/cluster-init/scripts/20-install-cyclecloud-multiregion-amlfs.sh"
    (workspace / script).parent.mkdir(parents=True)
    shutil.copyfile(ROOT / script, workspace / script)
    (workspace / "amlfs_flow/config.py").write_text("SECRET = 'never package'\n")
    (workspace / "parameters.json").write_text('{"password": "never package"}')
    metadata = builder.configparser.ConfigParser()
    metadata.read(workspace / "project.ini")
    version = metadata["project"]["version"]
    archive = builder.release(workspace, workspace / "dist", version)
    again = builder.release(workspace, workspace / "dist-again", version)
    assert archive.read_bytes() == again.read_bytes()
    assert archive.with_suffix(".gz.sha256").read_text() == (
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    prefix = "cyclecloud-multiregion-amlfs-" + version + "/"
    assert archive.name == "cyclecloud-multiregion-amlfs-" + version + ".tar.gz"
    with tarfile.open(archive) as project:
        assert set(project.getnames()) == {prefix + name for name in (
            "project.ini", "LICENSE", script,
            "blobs/cyclecloud-multiregion-amlfs.tar.gz",
            "blobs/cyclecloud-multiregion-amlfs.tar.gz.sha256",
        )}
        assert project.getmember(prefix + script).mode == 0o755
        payload = project.extractfile(prefix + "blobs/cyclecloud-multiregion-amlfs.tar.gz").read()
        checksum = project.extractfile(prefix + "blobs/cyclecloud-multiregion-amlfs.tar.gz.sha256").read()
        assert checksum.decode().startswith(hashlib.sha256(payload).hexdigest())
    for name in metadata.get("blobs", "Files").split(","):
        assert (workspace / "dist" / name.strip()).read_bytes() == (workspace / "blobs" / name.strip()).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(payload)) as runtime:
        assert "amlfs_flow/config.py" not in runtime.getnames()
        assert not any(name.startswith("ci/") for name in runtime.getnames())
        assert "scripts/install.py" in runtime.getnames()
        assert runtime.extractfile("amlfs_flow/__main__.py").read() == (ROOT / "amlfs_flow/__main__.py").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        builder.release(workspace, workspace / "dist", version)


@pytest.mark.parametrize("version, tag", [("0.2.0", "0.1.0"), ("0.2.0", "v0.2.0"), ("../bad", "../bad")])
def test_release_rejects_mismatched_tags_and_unsafe_versions_before_build(workspace, version, tag):
    builder = load_script("build_project")
    (workspace / "project.ini").write_text(
        "[project]\nname=cyclecloud-multiregion-amlfs\ntype=application\nversion=" + version + "\n")
    with pytest.raises(ValueError, match="version|tag"):
        builder.release(workspace, workspace / "dist", tag)
    assert not (workspace / "dist").exists()
    assert not (workspace / "specs").exists()
