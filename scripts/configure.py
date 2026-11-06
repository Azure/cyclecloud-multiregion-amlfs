#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Interactively prepare Azure CycleCloud Multi-region plugin for AMLFS deployment files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import URLError
from urllib.request import urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "cyclecloud.multi_region."
HEADER = re.compile(r"^\s*(\[+)([^\[\]\r\n]+)(\]+)\s*(?:#.*)?$")
ASSIGNMENT = re.compile(r"^\s*([\w.]+)\s*(:?=)\s*(.*?)\s*$")


class Section:
    """Keep original template text; only rewrite explicitly changed properties."""

    def __init__(self, name="", depth=0, header=""):
        self.name, self.depth, self.header = name, depth, header
        self.lines = []
        self.children = []

    def child(self, name):
        return next((child for child in self.children if child.name == name), None)

    def add(self, name):
        if self.child(name):
            raise ValueError("Duplicate template section: " + name)
        depth = self.depth + 1
        section = Section(name, depth, "\n" + "    " * (depth - 1)
                          + "[" * depth + name + "]" * depth + "\n")
        self.children.append(section)
        return section

    def properties(self):
        return {match[1]: match[3] for line in self.lines
                if (match := ASSIGNMENT.match(line))}

    def set(self, key, value):
        pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*:?=")
        matches = [i for i, line in enumerate(self.lines) if pattern.match(line)]
        if len(matches) > 1:
            raise ValueError("Duplicate property in " + self.name + ": " + key)
        line = "    " * self.depth + key + " = " + str(value) + "\n"
        if matches:
            self.lines[matches[0]] = line
        else:
            self.lines.append(line)

    def render(self):
        return self.header + "".join(self.lines) + "".join(
            child.render() for child in self.children)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def parse_template(text):
    root = Section()
    stack = [root]
    quote = None
    for line in text.splitlines(keepends=True):
        match = HEADER.match(line) if quote is None else None
        if match:
            depth = len(match[1])
            if depth != len(match[3]):
                raise ValueError("Unbalanced template section: " + line.strip())
            while stack[-1].depth >= depth:
                stack.pop()
            if depth != stack[-1].depth + 1:
                raise ValueError("Unsupported template nesting: " + line.strip())
            parent = stack[-1]
            section = parent.add(match[2].strip())
            section.header = line
            stack.append(section)
        else:
            stack[-1].lines.append(line)
        # Section-like text inside upstream multiline HTML/string values is data.
        if not line.lstrip().startswith("#"):
            for token in re.findall(r"'''|\"\"\"", line):
                if quote is None:
                    quote = token
                elif token == quote:
                    quote = None
    if quote:
        raise ValueError("Unterminated multiline template string")
    return root


def inspect_template(tree):
    clusters = [section for section in tree.children
                if section.name.startswith("cluster ")]
    if len(clusters) != 1:
        raise ValueError("Use a complete template containing exactly one cluster")
    cluster = clusters[0]
    for name in ("node defaults", "node scheduler", "node nodearraybase"):
        if not cluster.child(name):
            raise ValueError("Unsupported upstream layout: missing " + name)
    for key in ("Region", "SubnetId"):
        if cluster.child("node defaults").properties().get(key) != "$" + key:
            raise ValueError("Unsupported upstream layout: node defaults must use $" + key)
        if key in cluster.child("node scheduler").properties():
            raise ValueError("Scheduler has a separate " + key + "; review its home-region layout")
    specs = {section.name.split(":")[1] for section in cluster.walk()
             if section.name.startswith("cluster-init cyclecloud/slurm:")}
    if not {"default", "scheduler", "execute"}.issubset(specs):
        raise ValueError("Unsupported template: missing native Slurm cluster-init specs")
    definitions = {}
    for section in tree.walk():
        if section.name.startswith("parameter "):
            name = section.name[len("parameter "):]
            if name in definitions:
                raise ValueError("Duplicate template parameter: " + name)
            definitions[name] = section.properties()
        if ("cyclecloud-multi-region:" in section.name
                or any(NAMESPACE in line for line in section.lines)):
            raise ValueError("Use the original template, without this extension")
    for name in ("Region", "SubnetId", "Credentials", "ManagedIdentity", "HPCImageName"):
        if name not in definitions:
            raise ValueError("Unsupported upstream layout: missing parameter " + name)
    if any(name.startswith("MultiRegion") for name in definitions):
        raise ValueError("Template already contains MultiRegion parameters")
    return cluster, definitions


def checked(value, pattern, message):
    if not re.fullmatch(pattern, value):
        raise ValueError(message)
    return value


def release(value):
    return checked(value, r"v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*",
                   "Enter an exact upstream release tag, for example 4.0.11")


def positive(value):
    number = int(value)
    if number < 1:
        raise ValueError("Enter an integer of at least 1 (including the home region)")
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise ValueError("Enter a nonnegative integer")
    return number


def identifier(value):
    return checked(value, r"[A-Za-z][A-Za-z0-9_-]*",
                   "Use a letter followed by letters, digits, hyphens or underscores")


def subscription(value):
    return str(uuid.UUID(value))


def subnet(value):
    return checked(value, r"[A-Za-z0-9_.()-]+/[A-Za-z0-9_.()-]+/[A-Za-z0-9_.()-]+",
                   "Use resource-group/vnet/compute-subnet, not an ARM resource ID")


def absolute_path(value):
    checked(value, r"/[A-Za-z0-9_./-]+", "Use an absolute path without whitespace")
    if ".." in Path(value).parts or Path(value) == Path("/"):
        raise ValueError("Use a non-root absolute path without '..'")
    return value.rstrip("/")


def mount_path(value):
    value = absolute_path(value)
    if any(value == base or value.startswith(base + "/") for base in ("/sched", "/shared")):
        raise ValueError("Keep /sched and /shared separate from the regional data mount")
    return value


def yes_no(value):
    if value.lower() not in ("y", "yes", "n", "no"):
        raise ValueError("Enter yes or no")
    return value.lower() in ("y", "yes")


def ask(label, default=None, convert=str):
    while True:
        suffix = " [{}]".format(default) if default is not None else ""
        value = input(label + suffix + ": ").strip()
        if not value and default is not None:
            value = str(default)
        try:
            if not value or any(ord(char) < 32 for char in value):
                raise ValueError("A nonempty, single-line value is required")
            return convert(value)
        except ValueError as error:
            print("  " + str(error))


def region_letter(index):
    result = ""
    while index >= 0:
        index, digit = divmod(index, 26)
        result = chr(65 + digit) + result
        index -= 1
    return result


def download_template(version):
    url = ("https://raw.githubusercontent.com/Azure/cyclecloud-slurm/"
           + release(version) + "/templates/slurm.txt")
    try:
        with urlopen(url, timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except URLError as error:
        raise ValueError("Cannot fetch {}: {}. Check the release tag or use "
                         "--template-file for an offline copy.".format(url, error)) from error
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Upstream template exceeds the 2 MiB size limit")
    return data.decode("utf-8"), url


def home_arrays(cluster):
    arrays = []
    for node in cluster.children:
        if not node.name.startswith("nodearray "):
            continue
        properties = node.properties()
        config = node.child("configuration")
        role = config.properties().get("slurm.role") if config else None
        if properties.get("Extends") == "nodearraybase" or role == "login":
            if any(key in properties for key in ("Region", "SubnetId")):
                raise ValueError("Existing regional override in " + node.name
                                 + "; use an unextended home-region template")
            arrays.append(node)
    if not arrays:
        raise ValueError("Unsupported template: no native home nodearrays found")
    return arrays


def collect(cluster, definitions, count, parameters):
    parameters = dict(parameters)
    if any(key.startswith("MultiRegion") for key in parameters):
        raise ValueError("Use original parameters without generated MultiRegion settings")
    arrays = home_arrays(cluster)
    partitions = []
    for array in arrays:
        config = array.child("configuration")
        props = config.properties() if config else {}
        if props.get("slurm.role") == "login":
            continue
        partition = props.get("slurm.partition", array.name.split(" ", 1)[1])
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", partition):
            partitions.append(partition)

    print("\nExisting Blob/HSM, AMLFS, networking, Lustre clients and RBAC are prerequisites.")
    print("Native images, NFS, HA and other upstream settings are retained.")
    print("Workspace for Slurm normally reuses ccwLockerManagedIdentity; only add its roles.")
    if not parameters.get("Credentials"):
        parameters["Credentials"] = ask("Azure CycleCloud credential name (not a secret)")
    if not parameters.get("ManagedIdentity"):
        parameters["ManagedIdentity"] = ask(
            "Existing managed identity ARM ID (Workspace: ccwLockerManagedIdentity)",
            convert=lambda value: checked(
                value, r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/\s]+/"
                r"providers/Microsoft\.ManagedIdentity/userAssignedIdentities/[^/\s]+",
                "Enter the full existing user-assigned managed identity resource ID"))
    sub = ask("Default AMLFS subscription ID", convert=subscription)
    locker = ask("Uploaded cyclecloud-multi-region project locker")
    state = ask("Shared scheduler state directory", "/sched/cyclecloud-multi-region",
                absolute_path)

    def partition_list(value):
        names = [identifier(name.strip()) for name in value.split(",")]
        if len(set(names)) != len(names):
            raise ValueError("Partition names must be unique")
        return names

    home_partitions = ask("Existing home partitions for staged jobs (comma-separated)",
                          ",".join(dict.fromkeys(partitions)) or None, partition_list)
    print("These names map existing partitions; they do not create home partitions.")
    used_names = {node.name.split(" ", 1)[1] for node in cluster.children
                  if node.name.startswith(("node ", "nodearray "))}
    used_names.update(home_partitions)
    regions = []
    for index in range(count):
        letter = region_letter(index)
        key = "region" + letter
        print("\nRegion {}{} ({}/{})".format(letter, " - home" if index == 0 else "",
                                            index + 1, count))
        region = {"key": key, "letter": letter}
        region["location"] = ask("Azure location identifier",
                                 parameters.get("Region") if index == 0 else None,
                                 lambda value: checked(value, r"[a-z][a-z0-9]+",
                                                       "Use an Azure location identifier"))
        region["subscription_id"] = ask("AMLFS subscription ID", sub, subscription)
        region["resource_group"] = ask("AMLFS resource group")
        region["filesystem"] = ask("AMLFS resource name")
        region["source"] = ask(
            "Lustre mount source (MGS@tcp:/filesystem)",
            convert=lambda value: checked(value, r"[^\s]+@(?:tcp|o2ib)\d*:/[A-Za-z0-9_./-]+",
                                          "Use the full Lustre mount source, not just an IP"))
        region["mountpoint"] = ask("Regional data mountpoint", "/lustre", mount_path)
        region["subnet"] = ask("Compute subnet (not the dedicated AMLFS subnet)",
                               parameters.get("SubnetId") if index == 0 else None, subnet)
        if index:
            def unused_partition(value):
                identifier(value)
                if value in used_names:
                    raise ValueError("Partition/nodearray name is already used")
                return value

            region["partition"] = ask("New regional partition / nodearray",
                                       "multi-" + key, unused_partition)
            used_names.add(region["partition"])
            region["machine_type"] = ask("Regional VM size", "Standard_D4as_v5")
            image = parameters.get("HPCImageName",
                                   definitions["HPCImageName"].get("DefaultValue"))
            if isinstance(image, str) and image.startswith('"'):
                image = json.loads(image)
            if not isinstance(image, str) or image.startswith(("$", "=")):
                image = None
            region["image"] = ask("Regional image with a kernel-matched Lustre client", image)
            region["max_count"] = ask("Maximum nodes (initial count is always 0)", "1",
                                      nonnegative)
            region["hpc"] = ask("Enable native Slurm HPC mode?", "no", yes_no)
            region["use_pcpu"] = ask("Use physical CPUs rather than logical CPUs?",
                                     "yes" if region["hpc"] else "no", yes_no)
        regions.append(region)
    enabled = ask("\nEnable extension on deployment? Requires prepared infrastructure and roles",
                  "no", yes_no)
    return {
        "subscription": sub, "locker": locker, "state": state, "regions": regions,
        "home_partitions": home_partitions, "enabled": enabled, "parameters": parameters,
    }


def configure_template(tree, settings, version):
    cluster, _ = inspect_template(tree)
    arrays = home_arrays(cluster)
    project_version = next(line.split("=", 1)[1].strip()
                           for line in (ROOT / "project.ini").read_text().splitlines()
                           if line.startswith("version"))
    for section in tree.walk():
        if section.name.startswith("cluster-init cyclecloud/slurm:"):
            parts = section.name.split(":")
            if len(parts) == 3 and parts[-1] != version.lstrip("v"):
                raise ValueError("Native Slurm spec version conflicts with --slurm-version")
            if len(parts) == 2:
                original_name = section.name
                section.name += ":" + version.lstrip("v")
                section.header = section.header.replace(original_name, section.name, 1)
    group = tree.add("parameters Multi-Region").add("parameters Multi-region configuration")
    parameters = dict(settings["parameters"])

    def parameter(name, value, kind="String", default=None):
        section = group.add("parameter " + name)
        section.set("ParameterType", kind)
        if default is not None:
            section.set("DefaultValue", json.dumps(default))
        parameters[name] = value

    def config(node, values):
        section = node.child("configuration")
        if section is None:
            section = node.add("configuration")
        for key, value in values.items():
            section.set(NAMESPACE + key, value)

    defaults = cluster.child("node defaults")
    spec = defaults.add("cluster-init cyclecloud-multi-region:default:" + project_version)
    spec.set("SourceLocker", "$MultiRegionProjectLocker")
    spec.set("Order", 10000)
    # Unknown infrastructure nodes stay disabled even though they inherit the spec.
    config(defaults, {"enabled": "false", "role": "compute",
                      "mounts_json": '"[]"', "reconfigure": "false"})
    runtime = {
        "SUBSCRIPTION_ID": settings["subscription"],
        "REGIONS": {}, "HOME_REGION": "regionA",
        "PARTITION_REGIONS": dict.fromkeys(settings["home_partitions"], "regionA"),
        "STATE_DIR": settings["state"],
        "STAGEIN_CONFLICT_MODE": "Skip", "STAGEOUT_CONFLICT_MODE": "OverwriteAlways",
    }
    files = {}
    parameter("MultiRegionEnabled", settings["enabled"], "Boolean", False)
    parameter("MultiRegionProjectLocker", settings["locker"])
    for region in settings["regions"]:
        key, letter = region["key"], region["letter"]
        prefix = "MultiRegion" + letter
        runtime["REGIONS"][key] = {field: region[field] for field in (
            "subscription_id", "resource_group", "filesystem", "location", "mountpoint")}
        mounts = [{"source": region["source"], "mountpoint": region["mountpoint"],
                   "options": "defaults,_netdev"}]
        files["mounts-" + key + ".json"] = mounts
        parameter(prefix + "MountsJson", json.dumps(mounts), default="[]")
        if key == "regionA":
            parameters.update(Region=region["location"], SubnetId=region["subnet"])
            continue
        node = cluster.add("nodearray " + region["partition"])
        node.set("Extends", "nodearraybase")
        for suffix, field, prop, kind in (
            ("Location", "location", "Region", "String"),
            ("SubnetId", "subnet", "SubnetId", "String"),
            ("MachineType", "machine_type", "MachineType", "String"),
            ("ImageName", "image", "ImageName", "String"),
            ("MaxCount", "max_count", "MaxCount", "Integer"),
        ):
            parameter(prefix + suffix, region[field], kind)
            node.set(prop, "$" + prefix + suffix)
        node.set("InitialCount", 0)
        node.set("EnableTerminateNotification", "true")
        native = node.add("configuration")
        native.set("slurm.partition", region["partition"])
        native.set("slurm.default_partition", "false")
        native.set("slurm.hpc", str(region["hpc"]).lower())
        native.set("slurm.use_pcpu", str(region["use_pcpu"]).lower())
        config(node, {"enabled": "$MultiRegionEnabled", "mounts_json": "$" + prefix + "MountsJson"})
        runtime["PARTITION_REGIONS"][region["partition"]] = key
    parameter("MultiRegionConfigJson", json.dumps(runtime), default="{}")
    scheduler = cluster.child("node scheduler")
    config(scheduler, {"enabled": "$MultiRegionEnabled", "role": "scheduler",
                      "config_json": "$MultiRegionConfigJson", "mounts_json": "$MultiRegionAMountsJson"})
    for node in [scheduler, *arrays]:
        node.set("EnableTerminateNotification", "true")
        config(node, {"enabled": "$MultiRegionEnabled", "mounts_json": "$MultiRegionAMountsJson"})
    files["regions.json"] = runtime
    files["parameters.json"] = parameters
    return tree.render(), files


def write_bundle(output, files):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    created = []
    try:
        for name, text in files.items():
            path = output / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                created.append(path)
                stream.write(text)
    except BaseException:
        for path in created:
            path.unlink()
        output.rmdir()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slurm-version", type=release,
                        help="Exact Azure/cyclecloud-slurm release tag, not the Slurm daemon version")
    parser.add_argument("--regions", type=positive, help="Total regions, including regionA (home)")
    parser.add_argument("--template-file", type=Path,
                        help="Offline complete unextended template from the selected release")
    parser.add_argument("--parameters-file", type=Path,
                        help="Original deployment parameter JSON object to preserve")
    parser.add_argument("--output-dir", type=Path, help="New protected directory; never overwritten")
    args = parser.parse_args(argv)
    try:
        print("Azure CycleCloud Multi-region plugin for AMLFS configuration wizard")
        print("Local files only: no provisioning, role assignments, uploads or cluster changes.")
        version = args.slurm_version or ask("Upstream Slurm project release tag", convert=release)
        count = args.regions or ask("Total regions, including home", "4", positive)
        output = args.output_dir or Path(ask("New output directory",
                                            str(Path.home() / "cyclecloud-deploy/cyclecloud-multi-region")))
        output = output.expanduser().absolute()
        if output.exists() or output.is_symlink():
            raise ValueError("Output directory already exists; choose a new --output-dir")
        if args.template_file:
            original = args.template_file.read_bytes().decode("utf-8")
            source = str(args.template_file.resolve())
        else:
            original, source = download_template(version)
        tree = parse_template(original)
        cluster, definitions = inspect_template(tree)
        parameters = json.loads(args.parameters_file.read_text()) if args.parameters_file else {}
        if not isinstance(parameters, dict):
            raise ValueError("--parameters-file must contain a JSON object")
        settings = collect(cluster, definitions, count, parameters)
        template, data = configure_template(tree, settings, version)
        files = {name: json.dumps(value, indent=2) + "\n" for name, value in data.items()}
        files["slurm-upstream.txt"] = original
        files["slurm-multi-region.txt"] = template
        files["source.json"] = json.dumps({
            "slurm_project_version": version, "template_source": source,
            "template_sha256": hashlib.sha256(original.encode()).hexdigest(),
        }, indent=2) + "\n"
        print("\n{} regions; {} regional nodearrays; MultiRegionEnabled={}.".format(
            count, count - 1, settings["enabled"]))
        print("Output: " + str(output))
        if not ask("Write these local files?", "yes", yes_no):
            print("Cancelled; no files written.")
            return 0
        write_bundle(output, files)
        print("Created: " + ", ".join(sorted(files)))
        print("Review the upstream/template diff and all parameters before importing.")
        print("Check regional image/VM availability, shared NFS reachability, Lustre clients and RBAC.")
        print("Keep MultiRegionEnabled=false until prerequisites are ready. No cluster was changed.")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; no deployment performed.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
