# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Local wizard tests: no Azure credentials, network access or cluster mutations."""

import hashlib
import json
import re
from urllib.error import URLError

import pytest

from tests.test_project_install import load_script


TEMPLATE = '''# Site comment is retained.
[cluster Slurm]
Autoscale = $Autoscale
    [[node defaults]]
    Credentials = $Credentials
    Region = $Region
    SubnetId = $SubnetId
    Azure.Identities = $ManagedIdentity
        [[[configuration]]]
        cluster.identities.default = $ManagedIdentity
        slurm.additional.config = $additional_slurm_config
        [[[cluster-init cyclecloud/slurm:default]]]
        [[[configuration cyclecloud.mounts.nfs_shared]]]
        mountpoint = /shared
        address = $NFSAddress
    [[node scheduler]]
    MachineType = $SchedulerMachineType
        [[[configuration]]]
        slurm.role = scheduler
        [[[cluster-init cyclecloud/slurm:scheduler]]]
        [[[volume sched]]]
        Persistent = true
    [[nodearray scheduler-ha]]
    Extends = scheduler
        [[[configuration]]]
        slurm.is_primary_scheduler = false
    [[node nodearraybase]]
    Abstract = true
        [[[configuration]]]
        slurm.role = execute
        slurm.autoscale = true
        [[[cluster-init cyclecloud/slurm:execute]]]
        [[[network-interface eth0]]]
        AssociatePublicIpAddress = $ExecuteNodesPublic
    [[nodearray hpc]]
    Extends = nodearraybase
    MaxCount = $MaxHPCExecuteNodeCount
        [[[configuration]]]
        slurm.partition = hpc
        slurm.hpc = true
    [[nodearray dynamic]]
    Extends = nodearraybase
        [[[configuration]]]
        slurm.dynamic_feature := "dyn"
    [[nodearray login]]
        [[[configuration]]]
        slurm.role = login
        [[[cluster-init cyclecloud/slurm:login]]]
    [[node other-service]]
    MachineType = service-vm
[parameters Required]
    [[parameters Deployment]]
        [[[parameter Region]]]
        ParameterType = Cloud.Region
        [[[parameter SubnetId]]]
        ParameterType = Azure.Subnet
        [[[parameter Credentials]]]
        ParameterType = Cloud.Credentials
        [[[parameter ManagedIdentity]]]
        ParameterType = Azure.ManagedIdentity
        [[[parameter HPCImageName]]]
        DefaultValue = cycle.image.ubuntu24
        [[[parameter Description]]]
        Config.Template = \'''First line
[[not a real section]]
Last line\'''
'''


@pytest.fixture
def wizard():
    return load_script("configure")


def settings(wizard, count=4):
    return {
        "subscription": "00000000-0000-0000-0000-000000000001",
        "locker": "project-locker", "state": "/sched/multi-region",
        "home_partitions": ["hpc", "dynamic"], "enabled": False,
        "parameters": {"ManagedIdentity": ["keep-existing-identity"],
                       "additional_slurm_config": "AccountingStoreFlags=job_comment",
                       "configuration_slurm_ha_enabled": True,
                       "MaxHPCExecuteNodeCount": 7},
        "regions": [
            {
                "key": "region" + wizard.region_letter(index),
                "letter": wizard.region_letter(index),
                "location": "location" + str(index),
                "resource_group": "rg-" + str(index), "filesystem": "fs-" + str(index),
                "subscription_id": "00000000-0000-0000-0000-000000000001",
                "source": "10.{}.0.4@tcp:/lustrefs".format(index),
                "mountpoint": "/lustre", "subnet": "rg/vnet{}/compute".format(index),
                "partition": "multi-region" + wizard.region_letter(index),
                "machine_type": "Standard_D4as_v5", "image": "cycle.image.ubuntu24",
                "max_count": 1, "hpc": False, "use_pcpu": False,
            } for index in range(count)
        ],
    }


@pytest.mark.parametrize("count", [1, 4, 28])
def test_complete_template_and_json_for_arbitrary_regions(wizard, count):
    tree = wizard.parse_template(TEMPLATE)
    assert tree.render() == TEMPLATE
    values = settings(wizard, count)
    rendered, files = wizard.configure_template(tree, values, "4.0.11")
    parsed = wizard.parse_template(rendered)
    cluster = parsed.child("cluster Slurm")
    runtime = files["regions.json"]
    params = files["parameters.json"]
    assert len(runtime["REGIONS"]) == count
    assert json.loads(params["MultiRegionConfigJson"]) == runtime
    assert params["MultiRegionEnabled"] is False
    assert params["Region"] == runtime["REGIONS"]["regionA"]["location"]
    assert params["SubnetId"] == "rg/vnet0/compute"
    assert len([node for node in cluster.children if node.name.startswith("nodearray multi-region")]) == count - 1
    definitions = {section.name[len("parameter "):]: section.properties()
                   for section in parsed.walk() if section.name.startswith("parameter ")}
    for region in values["regions"]:
        prefix = "MultiRegion" + region["letter"]
        assert json.loads(params[prefix + "MountsJson"]) == files["mounts-" + region["key"] + ".json"]
        assert json.loads(definitions[prefix + "MountsJson"]["DefaultValue"]) == "[]"
        if region["key"] == "regionA":
            continue
        node = cluster.child("nodearray " + region["partition"])
        props = node.properties()
        config = node.child("configuration").properties()
        assert props["Extends"] == "nodearraybase"
        assert props["InitialCount"] == "0"
        assert params[props["MaxCount"][1:]] == 1
        assert params[props["Region"][1:]] == region["location"]
        assert params[props["SubnetId"][1:]] == region["subnet"]
        assert config[wizard.NAMESPACE + "mounts_json"] == "$" + prefix + "MountsJson"
        assert config["slurm.default_partition"] == "false"
        assert runtime["PARTITION_REGIONS"][config["slurm.partition"]] == region["key"]
    assert set(re.findall(r"\$(MultiRegion[A-Za-z]+)", rendered)).issubset(definitions)


def test_preserves_native_settings_and_isolates_roles(wizard):
    values = settings(wizard)
    rendered, files = wizard.configure_template(wizard.parse_template(TEMPLATE), values, "4.0.11")
    cluster = wizard.parse_template(rendered).child("cluster Slurm")
    for key, value in values["parameters"].items():
        assert files["parameters.json"][key] == value
    defaults = cluster.child("node defaults")
    assert defaults.properties()["Azure.Identities"] == "$ManagedIdentity"
    assert defaults.child("configuration cyclecloud.mounts.nfs_shared").properties()["mountpoint"] == "/shared"
    assert defaults.child("configuration").properties()[wizard.NAMESPACE + "enabled"] == "false"
    spec = defaults.child("cluster-init cyclecloud-multi-region:default:0.2.0")
    assert spec.properties() == {"SourceLocker": "$MultiRegionProjectLocker", "Order": "10000"}
    assert cluster.child("nodearray hpc").properties()["MaxCount"] == "$MaxHPCExecuteNodeCount"
    scheduler = cluster.child("node scheduler")
    assert scheduler.child("volume sched").properties()["Persistent"] == "true"
    assert scheduler.child("configuration").properties()[wizard.NAMESPACE + "role"] == "scheduler"
    assert cluster.child("nodearray scheduler-ha").properties()["Extends"] == "scheduler"
    assert cluster.child("nodearray login").child("configuration").properties()[
        wizard.NAMESPACE + "enabled"] == "$MultiRegionEnabled"
    assert cluster.child("node other-service").properties() == {"MachineType": "service-vm"}
    for role in ("default", "scheduler", "execute", "login"):
        assert "cluster-init cyclecloud/slurm:" + role + ":4.0.11" in rendered
    assert "BurstBufferType" not in rendered
    assert 'slurm.dynamic_feature := "dyn"' in rendered
    assert "[[not a real section]]" in rendered


@pytest.mark.parametrize("text, message", [
    ("[[node scheduler]]\n", "nesting"),
    ("[cluster Fragment]\n", "missing node defaults"),
    (TEMPLATE + "[cluster Other]\n", "exactly one"),
    (TEMPLATE.replace("nodearray hpc", "nodearray login"), "Duplicate template section"),
    (TEMPLATE.replace("parameter Description", "parameter MultiRegionEnabled"), "MultiRegion parameters"),
    (TEMPLATE.replace("cyclecloud/slurm:execute", "site/execute:default"), "native Slurm"),
    (TEMPLATE.replace("Region = $Region", "Region = otherlocation"), r"must use \$Region"),
    (TEMPLATE.replace("MachineType = $SchedulerMachineType",
                      "Region = otherlocation"), "Scheduler has a separate Region"),
])
def test_rejects_fragments_and_incompatible_templates(wizard, text, message):
    with pytest.raises(ValueError, match=message):
        wizard.inspect_template(wizard.parse_template(text))


def test_refuses_reextension_and_conflicting_versions(wizard):
    rendered, _ = wizard.configure_template(wizard.parse_template(TEMPLATE), settings(wizard), "4.0.11")
    with pytest.raises(ValueError, match="without this extension"):
        wizard.inspect_template(wizard.parse_template(rendered))
    with pytest.raises(ValueError, match="version conflicts"):
        wizard.configure_template(wizard.parse_template(TEMPLATE.replace(
            "slurm:default", "slurm:default:4.0.10")), settings(wizard), "4.0.11")
    regional = TEMPLATE.replace("MaxCount = $MaxHPCExecuteNodeCount",
                                "Region = $OtherRegion\n    MaxCount = $MaxHPCExecuteNodeCount")
    with pytest.raises(ValueError, match="regional override"):
        wizard.home_arrays(wizard.inspect_template(wizard.parse_template(regional))[0])


def test_prompt_retries_invalid_answers(wizard, monkeypatch, capsys):
    answers = iter(["0", "x", "5", "/subscriptions/not/a/subnet", "rg/vnet/compute"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert wizard.ask("Regions", convert=wizard.positive) == 5
    assert wizard.ask("Subnet", convert=wizard.subnet) == "rg/vnet/compute"
    assert "not an ARM resource ID" in capsys.readouterr().out
    for path in ("/", "/sched", "/shared/data", "/lustre/../shared"):
        with pytest.raises(ValueError):
            wizard.mount_path(path)
    for value in ("main", "../4.0.11", "26.05", ""):
        with pytest.raises(ValueError):
            wizard.release(value)


def test_download_is_pinned_and_reports_network_errors(wizard, monkeypatch):
    from io import BytesIO

    urls = []

    def fetch(url, timeout):
        urls.append(url)
        assert timeout == 30
        return BytesIO(TEMPLATE.encode())

    monkeypatch.setattr(wizard, "urlopen", fetch)
    text, source = wizard.download_template("4.0.11")
    assert text == TEMPLATE
    assert urls == [source]
    assert source.endswith("/Azure/cyclecloud-slurm/4.0.11/templates/slurm.txt")

    def fail(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(wizard, "urlopen", fail)
    with pytest.raises(ValueError, match="--template-file"):
        wizard.download_template("4.0.11")


def test_interactive_offline_wizard_and_protected_output(wizard, tmp_path, monkeypatch):
    template = tmp_path / "original.txt"
    original = TEMPLATE.replace("\n", "\r\n").encode()
    template.write_bytes(original)
    output = tmp_path / "generated"
    answers = iter([
        "cloud-credential",
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/"
        "providers/Microsoft.ManagedIdentity/userAssignedIdentities/ccwLockerManagedIdentity",
        "00000000-0000-0000-0000-000000000001", "locker", "", "",
        "locationa", "", "rg-a", "fs-a", "10.1.0.4@tcp:/lustrefs", "", "rg/vnet-a/compute",
        "locationb", "", "rg-b", "fs-b", "10.2.0.4@tcp:/lustrefs", "", "rg/vnet-b/compute",
        "hpc", "", "", "", "-1", "", "", "", "", "",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    args = ["--slurm-version", "4.0.11", "--regions", "2",
            "--template-file", str(template), "--output-dir", str(output)]
    assert wizard.main(args) == 0
    assert (output / "slurm-upstream.txt").read_bytes() == original
    assert json.loads((output / "source.json").read_text())["template_sha256"] == hashlib.sha256(original).hexdigest()
    params = json.loads((output / "parameters.json").read_text())
    assert params["MultiRegionEnabled"] is False
    assert params["MultiRegionBMaxCount"] == 1
    assert params["ManagedIdentity"].endswith("/ccwLockerManagedIdentity")
    assert set(json.loads(params["MultiRegionConfigJson"])["PARTITION_REGIONS"]) == {
        "hpc", "dynamic", "multi-regionB"}
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert wizard.main(args) == 1
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


def test_cancelled_prompt_writes_nothing(wizard, tmp_path, monkeypatch):
    template = tmp_path / "original.txt"
    template.write_text(TEMPLATE)
    output = tmp_path / "generated"

    def cancel(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", cancel)
    assert wizard.main(["--slurm-version", "4.0.11", "--regions", "2",
                        "--template-file", str(template), "--output-dir", str(output)]) == 130
    assert not output.exists()


def test_original_parameters_skip_identity_and_credential_prompts(wizard, monkeypatch):
    cluster, definitions = wizard.inspect_template(wizard.parse_template(TEMPLATE))
    original = {"Credentials": "keep-cloud", "ManagedIdentity": ["keep-identity"],
                "Region": "locationa", "SubnetId": "rg/vnet-a/compute",
                "configuration_slurm_ha_enabled": True, "NFSAddress": "keep-nfs"}
    answers = iter(["00000000-0000-0000-0000-000000000001", "locker", "", "",
                    "", "", "rg-a", "fs-a", "10.1.0.4@tcp:/lustrefs", "", "", ""])
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", respond)
    result = wizard.collect(cluster, definitions, 1, original)
    assert result["parameters"] == original
    assert result["regions"][0]["location"] == original["Region"]
    assert result["regions"][0]["subnet"] == original["SubnetId"]
    assert not any("credential name" in prompt or "identity ARM ID" in prompt for prompt in prompts)


def test_bundle_does_not_follow_existing_symlink(wizard, tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    link = tmp_path / "generated"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        wizard.write_bundle(link, {"parameters.json": "{}"})
    assert list(target.iterdir()) == []
