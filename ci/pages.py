#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Validate an existing GitHub Pages site before building or publishing."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def validated_site_url(repository, pages, expected_url=None):
    private = repository.get("private")
    if not ((private is True and repository.get("visibility") == "private")
            or (private is False and repository.get("visibility") == "public")):
        raise ValueError("Repository visibility must be explicitly private or public")
    if pages.get("public") is not (not private) or pages.get("build_type") != "workflow":
        raise ValueError(
            "Configure Settings > Pages with visibility matching the repository "
            "and GitHub Actions as the source. "
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
                + " and configure Source = GitHub Actions and visibility matching the repository, "
                "then rerun. "
                "Private Pages requires GitHub Enterprise Cloud. "
                "If the site is already configured, have an administrator check organization "
                "Pages policy and the workflow token's pages: read permission. "
                "A private repository alone does not make its Pages site private."
            ) from None
        site_url = validated_site_url(repository, pages, args.expected_url)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("site_url=" + site_url + "\n")
        print("Confirmed matching repository and Pages visibility and an Actions-managed site.")
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        message = str(error)
        annotation = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error title=Pages publication blocked::" + annotation, file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write("## Pages publication blocked\n\n" + message
                              + "\n\nNo Pages site was created or made public.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
