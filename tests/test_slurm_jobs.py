# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

import ast
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from tests.test_project_install import load_script


@pytest.fixture
def scheduler(monkeypatch, tmp_path):
    module = load_script("scheduler_jobs", directory="ci")
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="hpcadmin"))
    monkeypatch.setattr(module.os, "umask", lambda mode: None)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "Path", lambda path: tmp_path / str(path).lstrip("/")
                        if str(path).startswith("/lustre") else Path(path))
    return module


@pytest.fixture
def vm():
    return load_script("slurm_jobs", directory="ci")


@pytest.fixture
def settings():
    return {
        "run": "cyclecloud-multi-region-ci-123-1-abcdef123456", "home": "regionA",
        "regions": [
            {"key": "region" + letter, "location": "region" + letter.lower(),
             "partition": "htc" if letter == "A" else "multi-region" + letter,
             "source": "10.0." + str(index) + ".4@tcp:/lustrefs"}
            for index, letter in enumerate("ABCD")
        ],
    }


def successful_report(settings):
    return {"status": "succeeded", "user": "hpcadmin", "jobs": [
        {"region": region["key"], "partition": region["partition"],
         "job_id": str(index * 2 + iteration + 100), "state": "COMPLETED",
         "input_verified": True, "output_verified": True}
        for index, region in enumerate(settings["regions"]) for iteration in range(2)
    ]}


def fake_slurm(scheduler, monkeypatch, failure=None):
    calls, tasks = [], {}

    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "sbatch":
            if failure == "submission":
                raise subprocess.TimeoutExpired(args, 60)
            job_id = str(len(tasks) + 100)
            task = ast.literal_eval(re.search(r"^task = (.*)$", kwargs["input"], re.M)[1])
            assert args[1] == "--parsable"
            assert "--partition=" + task["partition"] in args
            assert "--chdir=/tmp" in args
            assert "#BB_LUA stage_in source=" + task["directory"] + " type=directory" in kwargs["input"]
            assert "#BB_LUA stage_out source=" + task["directory"] + "/output" in kwargs["input"]
            tasks[job_id] = task
            return job_id
        if args[:3] == ("scontrol", "show", "job"):
            job_id = args[3]
            task = tasks[job_id]
            if failure == "job":
                return f"JobId={job_id} UserId=hpcadmin(1000) Partition={task['partition']} JobState=FAILED ExitCode=1:0"
            if failure != "missing-output":
                directory = scheduler.Path(task["directory"])
                output = directory / "output"
                output.mkdir(exist_ok=True)
                data = (directory / "input/payload.bin").read_bytes()
                (output / "result.bin").write_bytes(data + task["tag"].encode())
                (output / "execution.json").write_text(json.dumps({
                    "user": "hpcadmin", "location": task["location"], "partition": task["partition"],
                    "job_id": job_id, "tag": task["tag"],
                    "sha256": "wrong" if failure == "checksum" else task["output_sha256"],
                }))
            return f"JobId={job_id} UserId=hpcadmin(1000) Partition={task['partition']} JobState=COMPLETED ExitCode=0:0"
        if args[0] == "scancel":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(scheduler, "command", command)
    return calls, tasks


def test_all_regions_run_two_isolated_jobs_and_verify_round_trips(scheduler, settings, monkeypatch, vm):
    calls, tasks = fake_slurm(scheduler, monkeypatch)
    report = scheduler.run_jobs(settings)
    vm.validate_report(report, settings)
    assert len(tasks) == 8
    assert len({task["directory"] for task in tasks.values()}) == 8
    assert len({task["input_sha256"] for task in tasks.values()}) == 8
    assert not any(call[0] == "scancel" for call in calls)


@pytest.mark.parametrize("failure", ["job", "checksum", "missing-output", "submission"])
def test_failures_block_success_and_cancel_only_this_runs_jobs(scheduler, settings, monkeypatch, failure):
    calls, tasks = fake_slurm(scheduler, monkeypatch, failure)
    clock = iter([0] * 18 + [5000] * 20)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    report = scheduler.run_jobs(settings)
    assert report["status"] == "failed"
    cancelled = [call for call in calls if call[0] == "scancel"]
    assert cancelled
    if failure == "submission":
        assert cancelled == [("scancel", "--name=" + settings["run"] + "-regionA-0", "--user=hpcadmin")]
    else:
        assert {call[1] for call in cancelled} == set(tasks)
        assert all(len(call) == 2 for call in cancelled)


def test_wrong_submit_user_rejected_before_jobs(scheduler, settings, monkeypatch):
    monkeypatch.setattr(scheduler.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="cyclecloud"))
    with pytest.raises(ValueError, match="hpcadmin"):
        scheduler.run_jobs(settings)


@pytest.mark.parametrize("fault", [None, "mount", "plugin", "installation", "retention"])
def test_preparation_checks_mount_and_hooks_before_creating_user_directory(
        scheduler, settings, tmp_path, monkeypatch, fault):
    marker = tmp_path / "installation.json"
    marker.write_text(json.dumps({"enabled": fault != "installation", "role": "scheduler"}))
    path = scheduler.Path
    monkeypatch.setattr(scheduler, "Path", lambda value:
                        marker if value == "/opt/cyclecloud-multi-region/installation.json" else path(value))
    monkeypatch.setattr(scheduler.os, "geteuid", lambda: 0)
    monkeypatch.setattr(scheduler.pwd, "getpwnam", lambda user: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    chowns = []
    monkeypatch.setattr(scheduler.os, "chown", lambda *args: chowns.append(args))
    (tmp_path / "lustre").mkdir()
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "findmnt":
            return settings["regions"][0]["source"] + (" ext4" if fault == "mount" else " lustre")
        if args[-2:] == ("show", "config"):
            return ("BurstBufferType = " + ("(null)" if fault == "plugin" else "burst_buffer/lua")
                    + "\nMinJobAge = " + ("300" if fault == "retention" else "7200") + " sec\n")
        return ""

    monkeypatch.setattr(scheduler, "command", command)
    if fault:
        with pytest.raises(ValueError):
            scheduler.prepare(settings)
        assert not chowns
    else:
        scheduler.prepare(settings)
        assert chowns == [(tmp_path / "lustre" / settings["run"], 1000, 1000)]
        assert ("scontrol", "reconfigure") in calls
        with pytest.raises(FileExistsError):
            scheduler.prepare(settings)


@pytest.mark.parametrize("field,value", [
    ("JobId", "101"), ("UserId", "root(0)"), ("Partition", "other"),
    ("JobState", "CANCELLED"), ("ExitCode", "0:9"),
])
def test_job_state_checks_identity_partition_and_exit_status(scheduler, field, value):
    fields = dict(JobId="100", UserId="hpcadmin(1000)", Partition="htc", JobState="COMPLETED", ExitCode="0:0")
    fields[field] = value
    with pytest.raises(ValueError):
        scheduler.job_state(" ".join(key + "=" + content for key, content in fields.items()), "100", "htc")


@pytest.mark.parametrize("fault", [None, "user", "region", "input", "mount"])
def test_compute_payload_checks_execution_and_input(scheduler, tmp_path, monkeypatch, fault):
    import urllib.request
    data = b"unique-input"
    (tmp_path / "input").mkdir()
    (tmp_path / "input/payload.bin").write_bytes(data)
    task = {"directory": str(tmp_path), "partition": "multi-regionB", "location": "regionb",
            "source": "10.0.1.4@tcp:/lustrefs", "tag": "regionB-0",
            "input_sha256": hashlib.sha256(b"wrong" if fault == "input" else data).hexdigest()}
    monkeypatch.setenv("SLURM_JOB_PARTITION", task["partition"])
    monkeypatch.setenv("SLURM_JOB_ID", "100")
    monkeypatch.setattr(scheduler.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_name="root" if fault == "user" else "hpcadmin"))
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *args, **kwargs: task["source"] + (" ext4" if fault == "mount" else " lustre"))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"regionc" if fault == "region" else b"regionb"

    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response()))
    if fault:
        with pytest.raises(ValueError):
            exec(compile(scheduler.payload_code(task), "<regional-job>", "exec"), {})
        assert not (tmp_path / "output").exists()
    else:
        exec(compile(scheduler.payload_code(task), "<regional-job>", "exec"), {})
        assert (tmp_path / "output/result.bin").read_bytes() == data + b"regionB-0"


@pytest.mark.parametrize("fault", ["missing", "duplicate", "input", "output", "state", "user"])
def test_incomplete_execution_or_transfer_report_is_not_success(vm, settings, fault):
    report = successful_report(settings)
    if fault == "missing":
        report["jobs"].pop()
    elif fault == "duplicate":
        report["jobs"][1]["job_id"] = report["jobs"][0]["job_id"]
    elif fault in ("input", "output"):
        report["jobs"][0][fault + "_verified"] = False
    elif fault == "state":
        report["jobs"][0]["state"] = "RUNNING"
    else:
        report["user"] = "cyclecloud"
    with pytest.raises(ValueError):
        vm.validate_report(report, settings)


def test_workload_selects_home_cpu_partition_and_every_region(vm, settings):
    runtime = {
        "HOME_REGION": "regionA",
        "REGIONS": {region["key"]: dict(location=region["location"], mountpoint="/lustre")
                    for region in settings["regions"]},
        "PARTITION_REGIONS": dict({"gpu": "regionA", "hpc": "regionA"},
                                 **{region["partition"]: region["key"] for region in settings["regions"]}),
    }
    outputs = {"regions": [dict(location=region["location"], mount_source=region["source"])
                           for region in settings["regions"]]}
    generated = vm.workload_settings(runtime, outputs, [r["location"] for r in settings["regions"]], "cyclecloud-multi-region-ci-123-1")
    assert generated["regions"] == settings["regions"]
    runtime["REGIONS"].pop("regionD")
    with pytest.raises(ValueError, match="every requested region"):
        vm.workload_settings(runtime, outputs, [r["location"] for r in settings["regions"]], "cyclecloud-multi-region-ci-123-1")


def test_scheduler_discovery_retries_until_ssh_and_slurm_are_ready(vm, tmp_path, monkeypatch):
    monkeypatch.setattr(vm.time, "sleep", lambda seconds: None)
    key = tmp_path / "cyclecloud.pem"
    key.touch()
    monkeypatch.setattr(vm, "KEY", str(key))
    nodes = iter([
        [{"Name": "scheduler", "Status": "Preparing", "Instance": {}}],
        [{"Name": "scheduler", "Status": "Ready", "Instance": {}}],
        [{"Name": "scheduler", "Status": "Ready", "Instance": {"PrivateIp": "192.0.2.4"}}],
        [{"Name": "scheduler", "Status": "Ready", "Instance": {"PrivateIp": "192.0.2.4"}}],
    ])
    def show_nodes(*args, **kwargs):
        assert args[args.index("-a") + 1] == "Name,Status,PrivateIp"
        return json.dumps(next(nodes))

    monkeypatch.setattr(vm, "command", show_nodes)
    calls = []

    def ssh(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(255, ["ssh"])
        return ""

    monkeypatch.setattr(vm, "ssh", ssh)
    assert vm.wait_scheduler("example-cluster", tmp_path) == "192.0.2.4"
    assert calls[-1][2:] == ("sudo", "-n", "scontrol", "ping")
    clock = iter([0, 1201])
    monkeypatch.setattr(vm.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError):
        vm.wait_scheduler("example-cluster", tmp_path)


def test_ready_scheduler_without_ssh_identity_fails_explicitly(vm, tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "KEY", str(tmp_path / "missing-key"))
    monkeypatch.setattr(vm, "command", lambda *args, **kwargs: json.dumps([
        {"Name": "scheduler", "Status": "Ready", "Instance": {"PrivateIp": "192.0.2.4"}},
    ]))
    with pytest.raises(ValueError, match="SSH identity is unavailable"):
        vm.wait_scheduler("example-cluster", tmp_path)


def test_ssh_uses_cyclecloud_key_but_submits_through_hpcadmin(vm, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(vm, "command", lambda *args, **kwargs: calls.append((args, kwargs)))
    vm.ssh("192.0.2.4", tmp_path, "sudo", "-n", "-iu", "hpcadmin", "/usr/bin/python3", "-", "run", "payload",
           input="source")
    args, kwargs = calls[0]
    assert args[:4] == ("ssh", "-T", "-i", "/opt/cycle_server/.ssh/cyclecloud.pem")
    assert args[-2] == "cyclecloud@192.0.2.4"
    assert shlex.split(args[-1])[:4] == ["sudo", "-n", "-iu", "hpcadmin"]
    assert "StrictHostKeyChecking=accept-new" in args
    assert "IdentitiesOnly=yes" in args
    assert kwargs["input"] == "source"
    with pytest.raises(ValueError):
        vm.ssh("192.0.2.4;unsafe", tmp_path, "true")


def test_vm_executes_preparation_then_hpcadmin_runner(vm, settings, tmp_path, monkeypatch):
    scenario = vm.scenario
    config = scenario.validate_config({"regions": [r["location"] for r in settings["regions"]]})
    context = dict(config=config, subscription="00000000-0000-0000-0000-000000000001",
                   repository="owner/repo", run_id="123", attempt="1")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(context))
    (tmp_path / "status").write_text("succeeded")
    (tmp_path / "private/cluster").mkdir(parents=True)
    (tmp_path / "private/cluster/regions.json").write_text("{}")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/infrastructure.plan.outputs.json").write_text("{}")
    (tmp_path / "deployment.json").write_text(json.dumps({
        "resourceGroup": {"value": "cyclecloud-multi-region-ci-123-1"},
        "subscriptionId": {"value": context["subscription"]},
        "clusterName": {"value": config["cluster_name"]},
    }))
    key = tmp_path / "identity"
    monkeypatch.setattr(vm, "KEY", str(key))
    monkeypatch.setattr(scenario, "VM_WORK", str(tmp_path))
    monkeypatch.setattr(scenario, "workspace_home", lambda config: tmp_path)
    monkeypatch.setattr(scenario, "owned_group", lambda *args: {})
    monkeypatch.setattr(scenario.provision, "Azure", lambda: SimpleNamespace(run=lambda *args: {}))
    monkeypatch.setattr(vm, "workload_settings", lambda *args: settings)
    monkeypatch.setattr(vm, "wait_scheduler", lambda *args: "192.0.2.4")
    monkeypatch.setattr(vm.os, "umask", lambda mode: None)
    calls = []
    monkeypatch.setattr(vm, "command", lambda *args, **kwargs: calls.append(args))

    def ssh(address, work, *remote, **kwargs):
        calls.append(remote)
        assert address == "192.0.2.4"
        assert json.loads(base64.b64decode(remote[-1])) == settings
        assert "def run_jobs(" in kwargs["input"]
        return "MULTI_REGION_JOBS:" + json.dumps(successful_report(settings))

    monkeypatch.setattr(vm, "ssh", ssh)
    assert vm.execute(settings_path) == 0
    assert calls[0] == (vm.CLI, "start_cluster", config["cluster_name"])
    assert calls[1][:6] == ("sudo", "-n", "/usr/bin/python3", "-I", "-", "prepare")
    assert calls[2][:8] == ("sudo", "-n", "-iu", "hpcadmin", "/usr/bin/python3", "-I", "-", "run")
    report = json.loads((tmp_path / "artifacts/jobs.json").read_text())
    assert report["status"] == "succeeded" and report["data_transfer_tested"] is True
    assert report["slurm_started"] is True
    assert len(json.dumps(report, separators=(",", ":"))) < 3500
    with pytest.raises(ValueError, match="already has a job report"):
        vm.execute(settings_path)
    (tmp_path / "artifacts/jobs.json").unlink()
    monkeypatch.setattr(scenario, "owned_group", lambda *args: None)
    calls.clear()
    assert vm.execute(settings_path) == 1
    assert calls == []
