#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Run isolated data round trips as hpcadmin on the CI Slurm scheduler."""

import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import time


USER = "hpcadmin"
JOB_TIMEOUT = 4800


def command(*args, timeout=60, **kwargs):
    return subprocess.check_output(list(args), text=True, timeout=timeout, **kwargs).strip()


def validate(settings):
    if not re.fullmatch(r"cyclecloud-multi-region-ci-\d+-\d+-[a-f0-9]{12}", settings["run"]):
        raise ValueError("Invalid isolated workload directory")
    regions = settings["regions"]
    if not regions or len({region["key"] for region in regions}) != len(regions):
        raise ValueError("Missing or duplicate workload regions")
    if len({region["location"] for region in regions}) != len(regions):
        raise ValueError("Workload must cover distinct Azure locations")
    for region in regions:
        for key in ("key", "partition", "location"):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", region[key]):
                raise ValueError("Invalid workload region or partition")
        if not re.fullmatch(r"[0-9.,:@a-zA-Z/_-]+", region["source"]):
            raise ValueError("Invalid expected Lustre mount source")
    if settings["home"] not in {region["key"] for region in regions}:
        raise ValueError("Home region is missing")


def prepare(settings):
    validate(settings)
    if os.geteuid() != 0:
        raise ValueError("Scheduler preparation requires sudo")
    account = pwd.getpwnam(USER)
    installation = json.loads(Path("/opt/cyclecloud-multi-region/installation.json").read_text())
    if installation.get("enabled") is not True or installation.get("role") != "scheduler":
        raise ValueError("Scheduler extension is not installed and enabled")
    home = next(region for region in settings["regions"] if region["key"] == settings["home"])
    if command("findmnt", "-rn", "--mountpoint", "/lustre", "-o", "SOURCE,FSTYPE").split() != [home["source"], "lustre"]:
        raise ValueError("Scheduler home AMLFS mount does not match the provisioned filesystem")
    command("scontrol", "reconfigure")
    config = command("scontrol", "show", "config")
    if not re.search(r"(?m)^\s*BurstBufferType\s*=\s*burst_buffer/lua\s*$", config):
        raise ValueError("Slurm Lua burst-buffer plugin is not active")
    retention = re.search(r"(?m)^\s*MinJobAge\s*=\s*(\d+)\b", config)
    if not retention or int(retention[1]) < JOB_TIMEOUT:
        raise ValueError("Slurm MinJobAge must cover the full CI job-validation window")
    root = Path("/lustre") / settings["run"]
    root.mkdir(mode=0o755)
    os.chown(root, account.pw_uid, account.pw_gid)


def payload_code(task):
    return f"""import hashlib,json,os,pathlib,pwd,subprocess,urllib.request
task = {task!r}
def require(condition, message):
    if not condition:
        raise ValueError(message)
require(pwd.getpwuid(os.geteuid()).pw_name == 'hpcadmin', 'Wrong execution user')
require(os.environ['SLURM_JOB_PARTITION'] == task['partition'], 'Wrong partition')
mount = subprocess.check_output(['findmnt','-rn','--mountpoint','/lustre','-o','SOURCE,FSTYPE'], text=True).split()
require(mount == [task['source'], 'lustre'], 'Wrong regional AMLFS')
request = urllib.request.Request('http://169.254.169.254/metadata/instance/compute/location?api-version=2021-02-01&format=text', headers={{'Metadata':'true'}})
with urllib.request.build_opener(urllib.request.ProxyHandler({{}})).open(request, timeout=10) as response:
    location = response.read().decode().strip()
require(location.lower() == task['location'].lower(), 'Wrong compute region')
root = pathlib.Path(task['directory'])
data = (root / 'input/payload.bin').read_bytes()
require(hashlib.sha256(data).hexdigest() == task['input_sha256'], 'Stage-in content mismatch')
output = root / 'output'
output.mkdir(exist_ok=True)
result = data + task['tag'].encode()
(output / 'result.bin').write_bytes(result)
(output / 'execution.json').write_text(json.dumps({{
    'user': pwd.getpwuid(os.geteuid()).pw_name, 'location': location.lower(),
    'partition': os.environ['SLURM_JOB_PARTITION'], 'job_id': os.environ['SLURM_JOB_ID'],
    'tag': task['tag'], 'sha256': hashlib.sha256(result).hexdigest(),
}}))
"""


def job_script(task):
    return (
        "#!/bin/bash\n"
        "#BB_LUA stage_in source=" + task["directory"] + " type=directory\n"
        "#BB_LUA stage_out source=" + task["directory"] + "/output type=directory\n"
        "set -euo pipefail\n"
        "/usr/bin/python3 -I - <<'MULTI_REGION_JOB'\n" + payload_code(task) + "MULTI_REGION_JOB\n"
    )


def job_state(text, job_id, partition):
    fields = dict(re.findall(r"(?:^|\s)([A-Za-z]+)=(\S+)", text))
    if (fields.get("JobId") != job_id or fields.get("Partition") != partition
            or not fields.get("UserId", "").startswith(USER + "(")):
        raise ValueError("Slurm job identity or partition mismatch")
    state = fields.get("JobState", "")
    if state not in {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "STAGE_OUT", "COMPLETED"}:
        raise ValueError("Slurm job " + job_id + " failed: " + state)
    if state == "COMPLETED" and fields.get("ExitCode") != "0:0":
        raise ValueError("Slurm job " + job_id + " returned a nonzero exit code")
    return state


def verify_output(task, job_id):
    output = Path(task["directory"]) / "output"
    if not (output / "result.bin").exists() or not (output / "execution.json").exists():
        return False
    result = (output / "result.bin").read_bytes()
    expected = task["output_sha256"]
    metadata = json.loads((output / "execution.json").read_text())
    if (hashlib.sha256(result).hexdigest() != expected or metadata != {
            "user": USER, "location": task["location"].lower(), "partition": task["partition"],
            "job_id": job_id, "tag": task["tag"], "sha256": expected}):
        raise ValueError("Returned stage-out content or execution metadata mismatch")
    return True


def run_jobs(settings, timeout=JOB_TIMEOUT):
    validate(settings)
    if pwd.getpwuid(os.geteuid()).pw_name != USER:
        raise ValueError("All Slurm submissions must run as hpcadmin")
    os.umask(0o022)
    os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    deadline = time.monotonic() + timeout
    root = Path("/lustre") / settings["run"]
    jobs = []
    report = {"status": "failed", "user": USER, "jobs": []}

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Timed out waiting for jobs and staged output on home AMLFS")
        return min(60, seconds)

    try:
        for region in settings["regions"]:
            for index in range(2):
                directory = root / (region["key"] + "-" + str(index))
                (directory / "input").mkdir(parents=True)
                (directory / "output").mkdir()
                data = os.urandom(65536)
                (directory / "input/payload.bin").write_bytes(data)
                task = dict(region, directory=str(directory), tag=directory.name,
                            input_sha256=hashlib.sha256(data).hexdigest(),
                            output_sha256=hashlib.sha256(data + directory.name.encode()).hexdigest())
                name = settings["run"] + "-" + directory.name
                # Record the unique name first so an ambiguous sbatch failure is cancellable.
                entry = {"name": name, "task": task, "job_id": None,
                         "slurm_completed": False, "completed": False}
                jobs.append(entry)
                result = command(
                    "sbatch", "--parsable", "--partition=" + region["partition"],
                    "--job-name=" + name, "--nodes=1", "--ntasks=1", "--cpus-per-task=1",
                    "--time=00:05:00", "--chdir=/tmp",
                    "--output=" + str(directory / "slurm-%j.out"),
                    input=job_script(task), timeout=remaining())
                if not re.fullmatch(r"\d+(?:;[A-Za-z0-9_.-]+)?", result):
                    raise ValueError("sbatch did not return a job ID")
                entry["job_id"] = result.split(";")[0]
        while not all(entry["completed"] for entry in jobs):
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for jobs and staged output on home AMLFS")
            for entry in jobs:
                if entry["completed"]:
                    continue
                task = entry["task"]
                if not entry["slurm_completed"]:
                    state = job_state(command("scontrol", "show", "job", entry["job_id"], "-o",
                                              timeout=remaining()),
                                      entry["job_id"], task["partition"])
                    entry["slurm_completed"] = state == "COMPLETED"
                if entry["slurm_completed"] and verify_output(task, entry["job_id"]):
                    entry["completed"] = True
                    report["jobs"].append({
                        "region": task["key"], "partition": task["partition"],
                        "job_id": entry["job_id"], "state": "COMPLETED",
                        "input_verified": True, "output_verified": True,
                    })
            if not all(entry["completed"] for entry in jobs):
                time.sleep(15)
        report["status"] = "succeeded"
    except Exception as error:
        report["error"] = str(error)[:400]
    finally:
        if report["status"] != "succeeded":
            for entry in jobs:
                if entry["completed"]:
                    continue
                selector = [entry["job_id"]] if entry["job_id"] else ["--name=" + entry["name"], "--user=" + USER]
                try:
                    command("scancel", *selector, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    report["cancellation_failed"] = True
    return report


def interrupted(signum, frame):
    raise TimeoutError("Regional job validation interrupted")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    settings = json.loads(base64.b64decode(sys.argv[2], validate=True))
    if sys.argv[1] == "prepare":
        prepare(settings)
    elif sys.argv[1] == "run":
        print("MULTI_REGION_JOBS:" + json.dumps(run_jobs(settings), separators=(",", ":")))
    else:
        raise ValueError("Unknown scheduler validation mode")
