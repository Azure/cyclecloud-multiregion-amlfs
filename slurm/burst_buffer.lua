--[[
  Slurm burst_buffer/lua hook for cross-region AMLFS data movement.

  Parses #BB stage_in / stage_out directives and dispatches to the
  Python helper at /opt/slurm-amlfs-flow/amlfs_flow.

  Archive serialization (one-at-a-time per filesystem) is enforced here
  via flock so the Python helper doesn't need filesystem write access
  for lock files.

  Directive format:
    #BB_LUA stage_in  source=/lustre/proj/input  type=directory
    #BB_LUA stage_out source=/lustre/proj/output type=directory
]]

local PYTHON = "/opt/slurm-amlfs-flow/venv/bin/python"
local MODULE = "amlfs_flow"
local LOCK_DIR = "/sched/slurm-amlfs-flow"

-- Partition that lives in the primary region (same as the scheduler's AMLFS).
-- Jobs in this partition skip staging entirely — they already have direct
-- access to the data on /lustre. Templated by scripts/install.sh.
local PRIMARY_PARTITION = "@PRIMARY_PARTITION@"

-- ── Helpers ────────────────────────────────────────────────────────────

local function log_info(msg)
    slurm.log_info("amlfs_flow: " .. msg)
end

local function log_error(msg)
    slurm.log_error("amlfs_flow: " .. msg)
end

-- Look up a job's partition via scontrol (slurm Lua API doesn't expose it).
local function job_partition(job_id)
    local handle = io.popen("scontrol show job " .. tostring(job_id) .. " -o 2>/dev/null")
    if not handle then return nil end
    local out = handle:read("*a") or ""
    handle:close()
    return out:match("Partition=([%w_-]+)")
end

-- Returns true if the job lives in the primary partition (no staging needed).
local function is_primary_partition(job_id)
    local p = job_partition(job_id)
    if not p then
        log_info("could not determine partition for job " .. tostring(job_id) .. " — assuming secondary")
        return false
    end
    if p == PRIMARY_PARTITION then
        log_info("job " .. tostring(job_id) .. " is in primary partition '" .. p .. "' — skipping staging")
        return true
    end
    return false
end

-- Read job script from file path (Slurm 25.05 passes path, not content)
local function read_script(script_path)
    log_info("read_script: path=" .. tostring(script_path))
    if not script_path or script_path == "" then
        log_error("read_script: empty path")
        return ""
    end
    local ok, f_or_err = pcall(io.open, script_path, "r")
    if not ok then
        log_error("read_script: io.open threw: " .. tostring(f_or_err))
        local handle = io.popen("cat " .. script_path .. " 2>&1")
        if handle then
            local out = handle:read("*a")
            handle:close()
            return out or ""
        end
        return ""
    end
    local f = f_or_err
    if not f then
        log_error("read_script: io.open returned nil for: " .. script_path)
        local handle = io.popen("cat " .. script_path .. " 2>&1")
        if handle then
            local out = handle:read("*a")
            handle:close()
            return out or ""
        end
        return ""
    end
    local script_content = f:read("*a")
    f:close()
    return script_content or ""
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

-- Acquire an exclusive flock, run the Python helper, then release.
-- This serializes archive operations per-filesystem from the scheduler side.
local function run_helper_with_lock(command, job_id, paths, lock_name)
    local path_args = ""
    for _, p in ipairs(paths) do
        path_args = path_args .. " " .. p
    end

    local lock_file = LOCK_DIR .. "/" .. lock_name .. ".lock"

    -- Use flock(1) to serialize: waits until lock is available, then runs command
    local cmd = string.format(
        "flock %s %s -m %s %s --job %s --paths %s 2>&1",
        lock_file, PYTHON, MODULE, command, tostring(job_id), path_args
    )

    log_info("Running (locked on " .. lock_name .. "): " .. cmd)
    local handle = io.popen(cmd)
    local output = handle:read("*a")
    local ok, _, exit_code = handle:close()

    if exit_code == 0 then
        log_info(command .. " succeeded for job " .. tostring(job_id))
        return slurm.SUCCESS, ""
    else
        log_error(command .. " FAILED for job " .. tostring(job_id) .. ": " .. (output or ""))
        return slurm.ERROR, output or ""
    end
end

local function run_teardown(job_id)
    local cmd = string.format(
        "%s -m %s teardown --job %s 2>&1",
        PYTHON, MODULE, tostring(job_id)
    )
    log_info("Running: " .. cmd)
    local handle = io.popen(cmd)
    local output = handle:read("*a")
    local ok, _, exit_code = handle:close()
    if exit_code ~= 0 then
        log_error("teardown warning for job " .. tostring(job_id) .. ": " .. (output or ""))
    end
    return slurm.SUCCESS, ""
end

-- ── Slurm BB Callbacks ────────────────────────────────────────────────

function slurm_bb_job_process(job_id, script_file, uid)
    log_info("job_process: job_id=" .. tostring(job_id))
    local job_script = read_script(script_file)
    local si, so = parse_directives(job_script)

    for _, p in ipairs(si) do
        if not p:match("^/lustre/") then
            log_error("Invalid stage_in path (must start with /lustre/): " .. p)
            return slurm.ERROR, "Invalid stage_in path: " .. p
        end
    end
    for _, p in ipairs(so) do
        if not p:match("^/lustre/") then
            log_error("Invalid stage_out path (must start with /lustre/): " .. p)
            return slurm.ERROR, "Invalid stage_out path: " .. p
        end
    end

    log_info("job_process: job_id=" .. tostring(job_id) .. " validated " .. #si .. " stage_in, " .. #so .. " stage_out directives")
    return slurm.SUCCESS, ""
end

function slurm_bb_setup(job_id, script_file, uid)
    log_info("setup: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

-- Stage-in: archive from primary AMLFS (lock on primary), then import into secondary AMLFS
function slurm_bb_data_in(job_id, script_file, uid)
    log_info("data_in: job_id=" .. tostring(job_id) .. " script=" .. tostring(script_file))

    if is_primary_partition(job_id) then
        return slurm.SUCCESS, ""
    end

    local job_script = read_script(script_file)
    local si, _ = parse_directives(job_script)
    log_info("data_in: found " .. #si .. " stage_in paths")

    if #si == 0 then
        log_info("data_in: no stage_in directives, skipping")
        return slurm.SUCCESS, ""
    end

    return run_helper_with_lock("stage-in", job_id, si, "archive-stagein")
end

function slurm_bb_pre_run(job_id, script_file, uid)
    log_info("pre_run: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

function slurm_bb_post_run(job_id, script_file, uid)
    log_info("post_run: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

-- Stage-out: archive from secondary AMLFS (lock on secondary), then import into primary AMLFS
function slurm_bb_data_out(job_id, script_file, uid)
    log_info("data_out: job_id=" .. tostring(job_id))

    if is_primary_partition(job_id) then
        return slurm.SUCCESS, ""
    end

    local job_script = read_script(script_file)
    local _, so = parse_directives(job_script)

    if #so == 0 then
        log_info("data_out: no stage_out directives, skipping")
        return slurm.SUCCESS, ""
    end

    return run_helper_with_lock("stage-out", job_id, so, "archive-stageout")
end

function slurm_bb_job_teardown(job_id, script_file, uid)
    log_info("teardown: job_id=" .. tostring(job_id))
    return run_teardown(job_id)
end

function slurm_bb_get_status()
    return slurm.SUCCESS, ""
end

-- ── Additional required callbacks (Slurm 25.05+) ──────────────────────

function slurm_bb_pools()
    local pools = '{"pools":[{"id":"azamlfs","quantity":1000,"granularity":1}]}'
    return slurm.SUCCESS, pools
end

function slurm_bb_test_data_in(job_id, script_file, uid)
    log_info("test_data_in: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

function slurm_bb_test_data_out(job_id, script_file, uid)
    log_info("test_data_out: job_id=" .. tostring(job_id))
    return slurm.SUCCESS, ""
end

function slurm_bb_real_size(job_id, script_file, uid)
    return slurm.SUCCESS, ""
end

function slurm_bb_paths(job_id, script_file, uid)
    return slurm.SUCCESS, ""
end
