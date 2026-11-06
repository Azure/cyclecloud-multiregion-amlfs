#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Require an existing private GitHub Pages site before building or publishing."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def private_site_url(repository, pages, expected_url=None):
    if repository.get("private") is not True or repository.get("visibility") != "private":
        raise ValueError("Refusing to publish documentation from a non-private repository")
    if pages.get("public") is not False or pages.get("build_type") != "workflow":
        raise ValueError(
            "Configure Settings > Pages with Private visibility and GitHub Actions as the source. "
            "Private Pages requires an organization with GitHub Enterprise Cloud."
        )
    site_url = pages.get("html_url")
    if not isinstance(site_url, str) or any(char.isspace() or ord(char) < 32 for char in site_url):
        raise ValueError("Pages did not provide a valid HTTPS site URL")
    url = urlsplit(site_url)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Pages did not provide a valid HTTPS site URL")
    if expected_url is not None and site_url != expected_url:
        raise ValueError("Pages URL changed after the build; rebuild before publishing")
    return site_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-url")
    args = parser.parse_args()
    try:
        endpoint = "repos/" + os.environ["GITHUB_REPOSITORY"]
        repository = json.loads(subprocess.check_output(
            ["gh", "api", endpoint], text=True, stderr=subprocess.PIPE))
        try:
            pages = json.loads(subprocess.check_output(
                ["gh", "api", endpoint + "/pages"], text=True, stderr=subprocess.PIPE))
        except subprocess.CalledProcessError as error:
            settings = (os.environ.get("GITHUB_SERVER_URL", "https://github.com")
                        + "/" + os.environ["GITHUB_REPOSITORY"] + "/settings/pages")
            reason = ("GitHub returned HTTP 404: the Pages site is missing or inaccessible."
                      if "HTTP 404" in (error.stderr or "")
                      else "GitHub denied or failed the Pages configuration request.")
            raise ValueError(
                reason + " Open " + settings
                + " and configure Source = GitHub Actions and Visibility = Private, then rerun. "
                "Private Pages requires GitHub Enterprise Cloud. "
                "If the site is already configured, have an administrator check organization "
                "Pages policy and the workflow token's pages: read permission. "
                "A private repository alone does not make its Pages site private."
            ) from None
        site_url = private_site_url(repository, pages, args.expected_url)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("site_url=" + site_url + "\n")
        print("Confirmed private repository and private, Actions-managed Pages site.")
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        message = str(error)
        annotation = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error title=Private Pages publication blocked::" + annotation, file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write("## Private Pages publication blocked\n\n" + message
                              + "\n\nNo Pages site was created or made public.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
