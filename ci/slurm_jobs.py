#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Drive the required regional Slurm checks from the owned CycleCloud VM."""

import base64
import ipaddress
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import four_region as scenario


KEY = "/opt/cycle_server/.ssh/cyclecloud.pem"
CLI = "/usr/local/bin/cyclecloud"


def command(*args, timeout=60, **kwargs):
    return subprocess.check_output(list(map(str, args)), text=True, timeout=timeout, **kwargs)


def workload_settings(runtime, outputs, locations, group):
    regions = []
    for key, region in runtime["REGIONS"].items():
        if region["mountpoint"] != "/lustre":
            raise ValueError("CI job checks require the generated /lustre mounts")
        partitions = sorted(partition for partition, target in runtime["PARTITION_REGIONS"].items()
                            if target == key)
        if not partitions:
            raise ValueError("No partition configured for " + key)
        partition = "htc" if key == runtime["HOME_REGION"] and "htc" in partitions else partitions[0]
        filesystem = next(item for item in outputs["regions"] if item["location"] == region["location"])
        regions.append({"key": key, "partition": partition, "location": region["location"],
                        "source": filesystem["mount_source"]})
    if len(regions) != len(locations) or {r["location"] for r in regions} != set(locations):
        raise ValueError("Generated runtime does not cover every requested region")
    return {"run": group + "-" + uuid.uuid4().hex[:12], "home": runtime["HOME_REGION"], "regions": regions}


def ssh(address, work, *remote, timeout=60, **kwargs):
    address = str(ipaddress.IPv4Address(address))
    return command(
        "ssh", "-T", "-i", KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=" + str(work / "job-known-hosts"),
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "cyclecloud@" + address, shlex.join(remote), timeout=timeout, **kwargs)


def wait_scheduler(name, work, timeout=1200):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = json.loads(command(CLI, "show_nodes", "-c", name, "--format=json",
                                   "-a", "Name,Status,PrivateIp"))
        schedulers = [node for node in nodes if node.get("Name") == "scheduler"]
        if len(schedulers) > 1:
            raise ValueError("Ambiguous scheduler")
        if (schedulers and schedulers[0].get("Status") == "Ready"
                and schedulers[0].get("Instance", {}).get("PrivateIp")):
            address = str(ipaddress.IPv4Address(schedulers[0]["Instance"]["PrivateIp"]))
            if not Path(KEY).is_file():
                raise ValueError("CycleCloud scheduler SSH identity is unavailable")
            try:
                # The extension writes this marker only after mounts and hooks are installed.
                ssh(address, work, "test", "-f", "/opt/cyclecloud-multi-region/installation.json")
                ssh(address, work, "sudo", "-n", "scontrol", "ping")
                return address
            except subprocess.SubprocessError:
                pass
        time.sleep(15)
    raise TimeoutError("Scheduler SSH or extension installation did not become ready")


def validate_report(report, settings):
    if report.get("status") != "succeeded" or report.get("user") != "hpcadmin":
        raise ValueError("Regional jobs failed: " + str(report.get("error", "incomplete run")))
    jobs = report.get("jobs", [])
    if len(jobs) != 2 * len(settings["regions"]) or len({job["job_id"] for job in jobs}) != len(jobs):
        raise ValueError("Missing or duplicate regional job results")
    for region in settings["regions"]:
        matches = [job for job in jobs if job["region"] == region["key"]]
        if len(matches) != 2 or any(
            job["partition"] != region["partition"] or job["state"] != "COMPLETED"
            or job.get("input_verified") is not True or job.get("output_verified") is not True
            for job in matches
        ):
            raise ValueError("Missing successful execution and data verification for " + region["key"])


def execute(settings_path):
    os.umask(0o077)
    work = Path(scenario.VM_WORK)
    report_path = work / "artifacts/jobs.json"
    if report_path.exists():
        raise ValueError("This CI scenario already has a job report; use a new run attempt")
    report = {"status": "failed", "user": "hpcadmin", "jobs": []}
    try:
        settings = json.loads(settings_path.read_text())
        config = scenario.validate_config(settings["config"])
        group = scenario.group_name(scenario.owner_tags(
            settings["repository"], settings["run_id"], settings["attempt"]))
        azure = scenario.provision.Azure()
        azure.run("login", "--identity")
        azure.subscription = str(uuid.UUID(settings["subscription"]))
        if scenario.owned_group(azure, group, settings["repository"]) is None:
            raise ValueError("Owned CI resource group is missing")
        if (work / "status").read_text().strip() != "succeeded":
            raise ValueError("VM bootstrap must complete before job validation")
        deployment = json.loads((scenario.workspace_home(config) / "deployment.json").read_text())
        if (deployment["resourceGroup"]["value"] != group
                or deployment["subscriptionId"]["value"].lower() != azure.subscription.lower()
                or deployment["clusterName"]["value"] != config["cluster_name"]):
            raise ValueError("Workspace does not describe the owned CI cluster")
        runtime = json.loads((work / "private/cluster/regions.json").read_text())
        outputs = json.loads((work / "artifacts/infrastructure.plan.outputs.json").read_text())
        workload = workload_settings(runtime, outputs, config["regions"], group)
        command(CLI, "start_cluster", config["cluster_name"], timeout=120)
        address = wait_scheduler(config["cluster_name"], work)
        encoded = base64.b64encode(json.dumps(workload).encode()).decode()
        source = (Path(__file__).parent / "scheduler_jobs.py").read_text()
        ssh(address, work, "sudo", "-n", "/usr/bin/python3", "-I", "-", "prepare", encoded, input=source, timeout=120)
        result = ssh(address, work, "sudo", "-n", "-iu", "hpcadmin", "/usr/bin/python3", "-I", "-",
                     "run", encoded, input=source, timeout=5100)
        lines = [line[len("MULTI_REGION_JOBS:"):] for line in result.splitlines() if line.startswith("MULTI_REGION_JOBS:")]
        if len(lines) != 1:
            raise ValueError("Scheduler did not return a job validation report")
        report = json.loads(lines[0])
        validate_report(report, workload)
        report["slurm_started"] = True
        report["data_transfer_tested"] = True
        return 0
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)[:400]
        return 1
    finally:
        pending = report_path.with_suffix(".pending")
        scenario.provision.write_json(pending, report)
        pending.rename(report_path)


if __name__ == "__main__":
    sys.exit(execute(Path(sys.argv[1])))
