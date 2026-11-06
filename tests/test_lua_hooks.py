# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Executable hook integration tests; no Slurm, Azure, or pytest required.

Run from the repository root: python3 -m unittest tests.test_lua_hooks -v
Uses a Lua 5.3/5.4 executable, or the installed shared interpreter. All
scratch files stay beneath the repository and are removed after each test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

from tests.test_lua_runner import library_name


ROOT = Path(__file__).resolve().parents[1]
MOCK_COMMAND = """#!{python}
import json
import os
from pathlib import Path
import sys

kind = "scontrol" if Path(sys.argv[0]).name == "scontrol" else "helper"
with open(os.environ["LUA_TEST_CALLS"], "a") as stream:
    stream.write(json.dumps([kind, sys.argv[1:]]) + "\\n")
print(os.environ["LUA_TEST_SCONTROL_OUTPUT"] if kind == "scontrol"
      else os.environ.get("LUA_TEST_HELPER_OUTPUT", ""))
sys.exit(int(os.environ.get("LUA_TEST_" + kind.upper() + "_EXIT", "0")))
"""


class LuaHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = next(
            (path for name in ("lua5.4", "lua5.3", "lua")
             if (path := shutil.which(name))),
            None,
        )
        if executable:
            cls.lua = [executable]
        elif library_name():
            cls.lua = [sys.executable, str(ROOT / "tests/test_lua_runner.py")]
        else:
            raise unittest.SkipTest("BLOCKED: no executable or shared Lua interpreter")

    def setUp(self):
        self.work = Path(".lua-hook-test-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)
        self.work = self.work.resolve()
        self.bin = self.work / "bin with 'quotes"
        self.bin.mkdir()
        self.helper = self.bin / "python ' helper"
        for executable in (self.helper, self.bin / "scontrol"):
            executable.write_text(MOCK_COMMAND.format(python=sys.executable))
            executable.chmod(0o755)
        hook = (ROOT / "slurm/burst_buffer.lua").read_text()
        hook = hook.replace('"@PYTHON@"', json.dumps(str(self.helper)))
        self.hook = self.work / "hook.lua"
        self.hook.write_text(hook)
        self.script = self.work / "job ' $(touch SCRIPT_PWNED).sh"
        self.script.write_text(
            "#!/bin/sh\n"
            "#BB_LUA stage_in source=/home-mount/input type=directory\n"
            "#BB_LUA stage_out source=/remote-mount/output type=directory\n"
        )
        self.calls_file = self.work / "calls.jsonl"
        self.env = dict(os.environ)
        self.env.update(
            PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            LUA_TEST_CALLS=str(self.calls_file),
            LUA_TEST_SCONTROL_OUTPUT="JobId=42 Partition=home.p",
        )

    def call(self, callback, *, expected=0, job="42", fallback=False, partition=None, uid="20001"):
        # SchedMD etc/burst_buffer.lua.example has identical contracts in
        # slurm-25-05-0-1 and slurm-26-05-4-1; all scalar arguments are strings.
        values = {
            "job": json.dumps(job),
            "script": json.dumps(str(self.script)),
            "uid": "nil" if uid is None else json.dumps(uid),
            "gid": '"20002"',
            "info": ("{job_id=42}" if partition is None
                     else "{job_id=42, partition=" + json.dumps(partition) + "}"),
            "pool": '"azamlfs"',
            "size": '"1"',
            "hurry": '"true"',
            "path_file": json.dumps(str(self.work / "paths.env")),
            "status_args": '"foo", "bar"',
        }
        contracts = {
            "slurm_bb_job_process": ("script", "uid", "gid", "info"),
            "slurm_bb_pools": (),
            "slurm_bb_setup": ("job", "uid", "gid", "pool", "size", "script", "info"),
            "slurm_bb_data_in": ("job", "script", "uid", "gid", "info"),
            "slurm_bb_test_data_in": ("job", "script", "uid", "gid", "info"),
            "slurm_bb_real_size": ("job", "uid", "gid", "info"),
            "slurm_bb_paths": ("job", "script", "path_file", "uid", "gid", "info"),
            "slurm_bb_pre_run": ("job", "script", "uid", "gid", "info"),
            "slurm_bb_post_run": ("job", "script", "uid", "gid", "info"),
            "slurm_bb_data_out": ("job", "script", "uid", "gid", "info"),
            "slurm_bb_test_data_out": ("job", "script", "uid", "gid", "info"),
            "slurm_bb_job_teardown": ("job", "script", "hurry", "uid", "gid"),
            "slurm_bb_get_status": ("uid", "gid", "status_args"),
        }
        arguments = ", ".join(values[name] for name in contracts[callback])
        harness = self.work / "invoke.lua"
        harness.write_text(
            "slurm = {SUCCESS=0, ERROR=1, log_info=function(msg) end,\n"
            "         log_error=function(msg) io.stderr:write(msg .. '\\n') end}\n"
            f"dofile({json.dumps(str(self.hook))})\n"
            + ("io.open = function() error('mock Slurm io.open restriction') end\n"
               if fallback else "")
            + f"local status, message = {callback}({arguments})\n"
            f"assert(status == {expected}, 'Unexpected status: ' .. tostring(status) "
            ".. ' message: ' .. tostring(message))\n"
            "assert(type(message) == 'string', 'Missing callback message')\n"
            "io.write(message)\n"
        )
        result = subprocess.run(
            self.lua + [str(harness)], cwd=self.work, env=self.env,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self):
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def test_three_partitions_are_explicit_for_both_stages(self):
        for partition in ("home.p", "west-gpu", "third_cpu"):
            for callback, command, path in (
                ("slurm_bb_data_in", "stage-in", "/home-mount/input"),
                ("slurm_bb_data_out", "stage-out", "/remote-mount/output"),
            ):
                with self.subTest(partition=partition, command=command):
                    self.env["LUA_TEST_SCONTROL_OUTPUT"] = f"JobId=42 Partition={partition}"
                    self.call(callback)
                    self.assertEqual(
                        self.calls()[-1],
                        ["helper", ["-I", "-m", "amlfs_flow", command, "--job", "42",
                                    "--partition", partition, "--uid", "20001", "--paths", path]],
                    )

    def test_lookup_errors_and_ambiguous_partitions_never_run_helper(self):
        cases = [
            ("", "0"), ("JobId=42", "0"),
            ("JobId=42 Partition=", "0"),
            ("JobId=42 Partition=(null)", "0"),
            ("JobId=42 Partition=N/A", "0"),
            ("JobId=42 Partition=home,west", "0"),
            ("JobId=42 Partition=home Partition=west", "0"),
            ("JobId=42 Partition=home\nJobId=43 Partition=west", "0"),
            ("JobId=42 Partition=home\nJobId=43", "0"),
            ("JobId=42 Partition=home JobId=43", "0"),
            ("Partition=home", "0"),
            ("JobId=42 Partition=home", "1"),
        ]
        for output, exit_code in cases:
            for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
                with self.subTest(output=output, callback=callback):
                    self.env["LUA_TEST_SCONTROL_OUTPUT"] = output
                    self.env["LUA_TEST_SCONTROL_EXIT"] = exit_code
                    result = self.call(callback, expected=1)
                    self.assertTrue(result.stdout)
        self.assertTrue(all(kind == "scontrol" for kind, _ in self.calls()))

    def test_invalid_submitter_uid_fails_without_helper(self):
        for uid in (None, "", "-1", "20001;false"):
            for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
                with self.subTest(uid=uid, callback=callback):
                    result = self.call(callback, uid=uid, partition="home.p", expected=1)
                    self.assertIn("submitter UID", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_permission_denial_is_returned_to_slurm(self):
        self.env["LUA_TEST_HELPER_EXIT"] = "1"
        self.env["LUA_TEST_HELPER_OUTPUT"] = "ERROR: Staging denied: submitter UID 20001 lacks read/traverse permission on '/lustre/input'"
        for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
            result = self.call(callback, expected=1, partition="home.p")
            self.assertIn("lacks read/traverse permission", result.stdout)

    def test_native_partition_snapshot_avoids_scontrol_for_both_stages(self):
        self.env["LUA_TEST_SCONTROL_EXIT"] = "1"
        for partition in ("home.p", "multi-regionB", "third_cpu"):
            for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
                with self.subTest(partition=partition, callback=callback):
                    self.call(callback, partition=partition)
                    kind, args = self.calls()[-1]
                    self.assertEqual(kind, "helper")
                    self.assertEqual(args[args.index("--partition") + 1], partition)
        self.assertTrue(all(kind == "helper" for kind, _ in self.calls()))

    def test_ambiguous_native_partition_does_not_fall_back_to_guessing(self):
        for partition in ("home,west", "", "(null)", "N/A", "home west"):
            for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
                with self.subTest(partition=partition, callback=callback):
                    self.call(callback, partition=partition, expected=1)
        self.assertEqual(self.calls(), [])

    def test_fallback_accepts_embedded_multiline_burst_buffer_directives(self):
        self.env["LUA_TEST_SCONTROL_OUTPUT"] = (
            "JobId=42 JobName=flow Partition=multi-regionB "
            "BurstBuffer=#BB_LUA stage_in source=/home-mount/input type=directory\n"
            "#BB_LUA stage_out source=/remote-mount/output type=directory "
            "WorkDir=/shared/home/example-user"
        )
        for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
            self.call(callback)
            kind, args = self.calls()[-1]
            self.assertEqual(kind, "helper")
            self.assertEqual(args[args.index("--partition") + 1], "multi-regionB")
        self.assertEqual([kind for kind, _ in self.calls()],
                         ["scontrol", "helper", "scontrol", "helper"])

    def test_unknown_partition_runtime_rejection_is_not_local_success(self):
        self.env["LUA_TEST_SCONTROL_OUTPUT"] = "JobId=42 Partition=unknown"
        self.env["LUA_TEST_HELPER_EXIT"] = "2"
        self.env["LUA_TEST_HELPER_OUTPUT"] = "Unknown Slurm partition: unknown"
        result = self.call("slurm_bb_data_in", expected=1)
        self.assertIn("Unknown Slurm partition", result.stdout)
        self.assertIn("unknown", self.calls()[-1][1])

    def test_shell_safe_paths_metadata_config_and_script_fallback(self):
        paths = [
            "/custom/input';touch${IFS}PWNED;#",
            "/custom/$(touch${IFS}PWNED2)",
            "/custom/`touch${IFS}PWNED3`",
            "/custom/*?[$HOME]",
        ]
        self.script.write_text("".join(
            f"#BB_LUA stage_in source={path} type=directory\n" for path in paths
        ))
        job = "42'; touch JOB_PWNED; echo '"
        self.env["LUA_TEST_SCONTROL_OUTPUT"] = "JobId=42 Partition=west';touch${IFS}PART_PWNED;#"
        self.call("slurm_bb_job_process", job=job, fallback=True)
        self.call("slurm_bb_data_in", job=job, fallback=True)
        calls = self.calls()
        self.assertEqual(calls[0], ["scontrol", ["show", "job", job, "-o"]])
        args = calls[1][1]
        self.assertEqual(args[args.index("--job") + 1], job)
        self.assertEqual(args[args.index("--partition") + 1], "west';touch${IFS}PART_PWNED;#")
        self.assertEqual(args[args.index("--paths") + 1:], paths)
        self.call("slurm_bb_job_teardown", job=job)
        self.assertEqual(self.calls()[-1][1][-1], job)
        self.assertFalse(any("PWNED" in path.name for path in self.work.iterdir()
                             if path != self.script))

    def test_stage_nonzero_is_error_but_teardown_remains_best_effort(self):
        self.env["LUA_TEST_HELPER_EXIT"] = "7"
        self.env["LUA_TEST_HELPER_OUTPUT"] = "archive failure details"
        for callback in ("slurm_bb_data_in", "slurm_bb_data_out"):
            result = self.call(callback, expected=1)
            self.assertIn("archive failure details", result.stdout)
            self.assertIn("exit 7", result.stdout)
        result = self.call("slurm_bb_job_teardown")
        self.assertIn("teardown warning", result.stderr)
        self.assertEqual(self.calls()[-1][1], ["-I", "-m", "amlfs_flow", "teardown", "--job", "42"])

    def test_missing_helper_is_a_stage_error(self):
        self.helper.unlink()
        result = self.call("slurm_bb_data_in", expected=1)
        self.assertIn("exit 127", result.stdout)
        self.assertEqual([kind for kind, _ in self.calls()], ["scontrol"])

    def test_missing_script_is_error_and_no_directives_is_noop(self):
        self.script.unlink()
        for callback in ("slurm_bb_job_process", "slurm_bb_data_in", "slurm_bb_data_out"):
            self.call(callback, expected=1)
        self.script.write_text("#!/bin/sh\ntrue\n")
        for callback in ("slurm_bb_job_process", "slurm_bb_data_in", "slurm_bb_data_out"):
            self.call(callback)
        self.assertEqual(self.calls(), [])

    def test_absolute_mount_independent_paths_and_relative_rejection(self):
        self.call("slurm_bb_job_process")
        for direction in ("stage_in", "stage_out"):
            self.script.write_text(f"#BB_LUA {direction} source=relative/path type=directory\n")
            self.call("slurm_bb_job_process", expected=1)
        self.assertEqual(self.calls(), [])

    def test_submission_reads_first_argument_not_uid_filename(self):
        decoy = self.work / "20001"
        decoy.write_text("#BB_LUA stage_in source=invalid/uid-file type=directory\n")
        self.call("slurm_bb_job_process")
        decoy.write_text("#!/bin/sh\n")
        self.script.write_text("#BB_LUA stage_in source=relative/input type=directory\n")
        result = self.call("slurm_bb_job_process", expected=1)
        self.assertIn("relative/input", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_scheduler_required_callbacks_execute(self):
        for callback in (
            "slurm_bb_setup", "slurm_bb_pre_run", "slurm_bb_post_run",
            "slurm_bb_get_status", "slurm_bb_pools", "slurm_bb_test_data_in",
            "slurm_bb_test_data_out", "slurm_bb_real_size", "slurm_bb_paths",
        ):
            with self.subTest(callback=callback):
                result = self.call(callback)
                if callback == "slurm_bb_pools":
                    self.assertEqual(json.loads(result.stdout)["pools"][0]["id"], "azamlfs")
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
