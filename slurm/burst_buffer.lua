-- Copyright (c) Microsoft Corporation.
-- SPDX-License-Identifier: MIT

--[[
  Slurm burst_buffer/lua hook for cross-region AMLFS data movement.

  Parses #BB stage_in / stage_out directives and dispatches to the
  Python helper with the job's resolved Slurm partition. The runtime owns
  regional routing, local no-ops, mount boundaries, and source-filesystem
  locking for the entire stage operation.

  Directive format:
    #BB_LUA stage_in  source=/lustre/proj/input  type=directory
    #BB_LUA stage_out source=/lustre/proj/output type=directory
]]

-- Installer inserts a Lua-string-escaped interpreter path.
local PYTHON = "@PYTHON@"
local MODULE = "amlfs_flow"
local SCONTROL = "scontrol"

-- ── Helpers ────────────────────────────────────────────────────────────

local function log_info(msg)
    slurm.log_info("amlfs_flow: " .. msg)
end

local function log_error(msg)
    slurm.log_error("amlfs_flow: " .. msg)
end

local function shell_quote(value)
    return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function run_command(args)
    local quoted = {}
    for _, arg in ipairs(args) do
        if tostring(arg):find("%z") then
            return nil, "Command argument contains a NUL byte"
        end
        table.insert(quoted, shell_quote(arg))
    end
    local cmd = table.concat(quoted, " ") .. " 2>&1"
    log_info("Running: " .. cmd)
    local opened, handle = pcall(io.popen, cmd)
    if not opened or not handle then
        return nil, "Could not execute command: " .. tostring(handle)
    end
    local output = handle:read("*a") or ""
    local ok, _, exit_code = handle:close()
    if not ok or (exit_code and exit_code ~= 0) then
        return nil, "Command failed (exit " .. tostring(exit_code) .. "): " .. output
    end
    return output
end

-- scontrol may return a comma-separated candidate list before scheduling.
-- Never guess a region until exactly one actual partition is available.
local function single_partition(partition)
    if type(partition) ~= "string" or partition == ""
        or partition:find("[,%s%z]") or partition == "(null)"
        or partition == "N/A" then
        return nil, "Could not determine a single job partition"
    end
    return partition
end

local function job_partition(job_id, job_info)
    -- SchedMD's _lua_job_info_field exposes the native partition snapshot.
    -- Use it directly rather than making another controller RPC.
    if job_info and job_info.partition ~= nil then
        return single_partition(job_info.partition)
    end
    local out, err = run_command({SCONTROL, "show", "job", tostring(job_id), "-o"})
    if not out then return nil, err end
    local partition
    local records = 0
    -- -o preserves newlines embedded in BurstBuffer directive values.
    -- Count job records, not physical lines.
    for token in out:gmatch("%S+") do
        if token:match("^JobId=") then records = records + 1 end
        local value = token:match("^Partition=(.*)$")
        if value then
            if partition then return nil, "Ambiguous job partition" end
            partition = value
        end
    end
    if records ~= 1 then
        return nil, "Could not determine a single job record"
    end
    return single_partition(partition)
end

-- Read job script from file path (Slurm 25.05 passes path, not content)
local function read_script(script_path)
    log_info("read_script: path=" .. tostring(script_path))
    if not script_path or script_path == "" then
        log_error("read_script: empty path")
        return nil, "Empty job script path"
    end
    local ok, f_or_err = pcall(io.open, script_path, "r")
    if not ok then
        log_error("read_script: io.open threw: " .. tostring(f_or_err))
        return run_command({"cat", "--", script_path})
    end
    local f = f_or_err
    if not f then
        log_error("read_script: io.open returned nil for: " .. script_path)
        return run_command({"cat", "--", script_path})
    end
    local script_content, err = f:read("*a")
    f:close()
    return script_content, err
end

-- Parse #BB directives from the job script.
local function parse_directives(job_script)
    local stage_in = {}
    local stage_out = {}

    if not job_script or job_script == "" then
        return stage_in, stage_out
    end

    for line in job_script:gmatch("[^\n]+") do
        local src = line:match("^#BB_LUA%s+stage_in%s+.*source=(%S+)")
        if src then
            table.insert(stage_in, src)
        end

        src = line:match("^#BB_LUA%s+stage_out%s+.*source=(%S+)")
        if src then
            table.insert(stage_out, src)
        end
    end

    return stage_in, stage_out
end

local function run_helper(command, job_id, paths, partition, uid)
    if uid == nil or not tostring(uid):match("^%d+$") then
        local err = "Cannot stage data: missing or invalid job submitter UID"
        log_error(err)
        return slurm.ERROR, err
    end
    local args = {PYTHON, "-I", "-m", MODULE, command, "--job", tostring(job_id),
                  "--partition", partition, "--uid", tostring(uid), "--paths"}
    for _, p in ipairs(paths) do
        table.insert(args, p)
    end

    local output, err = run_command(args)
    if output then
        log_info(command .. " succeeded for job " .. tostring(job_id))
        return slurm.SUCCESS, ""
    else
        log_error(command .. " FAILED for job " .. tostring(job_id) .. ": " .. err)
        return slurm.ERROR, err
    end
end

local function run_teardown(job_id)
    local output, err = run_command({PYTHON, "-I", "-m", MODULE, "teardown", "--job", tostring(job_id)})
    if not output then
        log_error("teardown FAILED for job " .. tostring(job_id) .. ": " .. err)
        return slurm.ERROR, err
    end
    return slurm.SUCCESS, ""
end

-- ── Slurm BB Callbacks ────────────────────────────────────────────────

-- Contracts match SchedMD's etc/burst_buffer.lua.example in both
-- slurm-25-05-0-1 and slurm-26-05-4-1. Submission has no job_id argument.
function slurm_bb_job_process(script_file, uid, gid, job_info)
    log_info("job_process: script=" .. tostring(script_file) .. " uid=" .. tostring(uid))
    local job_script, err = read_script(script_file)
    if not job_script then return slurm.ERROR, err end
    local si, so = parse_directives(job_script)

    for _, p in ipairs(si) do
        if not p:match("^/") then
            log_error("Invalid stage_in path (must be absolute): " .. p)
            return slurm.ERROR, "Invalid stage_in path: " .. p
        end
    end
    for _, p in ipairs(so) do
        if not p:match("^/") then
            log_error("Invalid stage_out path (must be absolute): " .. p)
            return slurm.ERROR, "Invalid stage_out path: " .. p
        end
    end

    log_info("job_process: validated " .. #si .. " stage_in, " .. #so .. " stage_out directives")
    return slurm.SUCCESS, ""
end

function slurm_bb_setup(job_id, uid, gid, pool, bb_size, script_file, job_info)
    log_info("setup: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

-- Stage-in: the runtime resolves HOME_REGION -> partition region.
function slurm_bb_data_in(job_id, script_file, uid, gid, job_info)
    log_info("data_in: job_id=" .. tostring(job_id) .. " script=" .. tostring(script_file))

    local job_script, err = read_script(script_file)
    if not job_script then return slurm.ERROR, err end
    local si, _ = parse_directives(job_script)
    log_info("data_in: found " .. #si .. " stage_in paths")

    if #si == 0 then
        log_info("data_in: no stage_in directives, skipping")
        return slurm.SUCCESS, ""
    end

    local partition, partition_err = job_partition(job_id, job_info)
    if not partition then
        log_error(partition_err)
        return slurm.ERROR, partition_err
    end
    return run_helper("stage-in", job_id, si, partition, uid)
end

function slurm_bb_pre_run(job_id, script_file, uid, gid, job_info)
    log_info("pre_run: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

function slurm_bb_post_run(job_id, script_file, uid, gid, job_info)
    log_info("post_run: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

-- Stage-out: the runtime resolves partition region -> HOME_REGION.
function slurm_bb_data_out(job_id, script_file, uid, gid, job_info)
    log_info("data_out: job_id=" .. tostring(job_id))

    local job_script, err = read_script(script_file)
    if not job_script then return slurm.ERROR, err end
    local _, so = parse_directives(job_script)

    if #so == 0 then
        log_info("data_out: no stage_out directives, skipping")
        return slurm.SUCCESS, ""
    end

    local partition, partition_err = job_partition(job_id, job_info)
    if not partition then
        log_error(partition_err)
        return slurm.ERROR, partition_err
    end
    return run_helper("stage-out", job_id, so, partition, uid)
end

function slurm_bb_job_teardown(job_id, script_file, hurry, uid, gid)
    log_info("teardown: job_id=" .. tostring(job_id))
    return run_teardown(job_id)
end

function slurm_bb_get_status(uid, gid, ...)
    return slurm.SUCCESS, ""
end

-- ── Additional required callbacks (Slurm 25.05+) ──────────────────────

function slurm_bb_pools()
    local pools = '{"pools":[{"id":"azamlfs","quantity":1000,"granularity":1}]}'
    return slurm.SUCCESS, pools
end

function slurm_bb_test_data_in(job_id, script_file, uid, gid, job_info)
    log_info("test_data_in: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

function slurm_bb_test_data_out(job_id, script_file, uid, gid, job_info)
    log_info("test_data_out: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

function slurm_bb_real_size(job_id, uid, gid, job_info)
    return slurm.SUCCESS, ""
end

function slurm_bb_paths(job_id, script_file, path_file, uid, gid, job_info)
    return slurm.SUCCESS, ""
end
