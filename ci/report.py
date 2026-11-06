#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Publish only allowlisted E2E outcomes, never deployment diagnostics."""

import os
from pathlib import Path
import sys


PHASES = (
    ("configuration", "Configuration"),
    ("authentication", "Authentication"),
    ("build", "Native project build"),
    ("workspace", "Workspace deployment"),
    ("installation", "Regional setup"),
    ("workloads", "Workload and data validation"),
)
LABELS = {
    "success": "✅ Passed",
    "failure": "❌ Failed",
    "cancelled": "⚪ Cancelled",
    "skipped": "⏭️ Not run",
    "unknown": "⚪ Not verified",
}


def render_report(deploy, outcomes):
    keys = [key for key, _ in PHASES] + ["scenario", "cleanup"]
    states = {}
    valid = True
    for key in keys:
        value = outcomes.get(key, "")
        if value == "":
            states[key] = "skipped"
        elif isinstance(value, str) and value in LABELS and value != "unknown":
            states[key] = value
        else:
            states[key] = "unknown"
            valid = False

    rows = []
    if deploy:
        rows.extend((label, LABELS[states[key]]) for key, label in PHASES)
        regional_result = ("✅ 2/2 jobs; input and output verified"
                           if states["workloads"] == "success" else "⚪ Not verified")
        rows.extend(("Region " + letter, regional_result) for letter in "ABCD")
        if states["scenario"] == "success" and any(states[key] != "success" for key, _ in PHASES):
            valid = False
        relevant = [states["scenario"], states["cleanup"]]
        title = "E2E report"
    else:
        relevant = [states["cleanup"]]
        title = "Cleanup report"
    rows.append(("Cleanup", LABELS[states["cleanup"]]))
    if not valid:
        overall = "⚪ Incomplete"
    elif "failure" in relevant:
        overall = LABELS["failure"]
    elif "cancelled" in relevant:
        overall = LABELS["cancelled"]
    elif relevant[0] == "success" and all(value in ("success", "skipped") for value in relevant):
        overall = LABELS["success"]
    else:
        overall = LABELS["skipped"]
    rows.append(("Overall", overall))
    text = "## " + title + "\n\n| Check | Result |\n| --- | --- |\n"
    text += "".join("| " + label + " | " + result + " |\n" for label, result in rows)
    return text, valid


def main():
    keys = [key for key, _ in PHASES] + ["scenario", "cleanup"]
    outcomes = {key: os.environ.get("RESULT_" + key.upper(), "") for key in keys}
    text, valid = render_report(os.environ.get("REPORT_DEPLOY") == "true", outcomes)
    try:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(text)
    except (KeyError, OSError):
        print("Unable to write the sanitized workflow summary.", file=sys.stderr)
        return 1
    print(text, end="")
    if not valid:
        print("Workflow summary inputs were incomplete or invalid; details are not published.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
