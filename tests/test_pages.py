# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

import json
import subprocess
import sys

import pytest

from tests.test_project_install import load_script


@pytest.fixture
def pages():
    return load_script("pages", directory="ci")


def site():
    return dict(public=False, build_type="workflow",
                html_url="https://example.pages.github.io/")


def test_private_site_supports_root_and_project_urls(pages):
    repository = {"private": True, "visibility": "private"}
    for url in ("https://example.pages.github.io/", "https://docs.example.com/project/"):
        settings = dict(site(), html_url=url)
        assert pages.private_site_url(repository, settings, url) == url


@pytest.mark.parametrize("repository,settings", [
    ({"private": False, "visibility": "public"}, site()),
    ({"private": True, "visibility": "internal"}, site()),
    ({"private": True}, site()),
    ({"private": True, "visibility": "private"}, {"build_type": "workflow"}),
    ({"private": True, "visibility": "private"}, dict(site(), public=True)),
    ({"private": True, "visibility": "private"}, dict(site(), public="false")),
    ({"private": True, "visibility": "private"}, dict(site(), build_type="legacy")),
])
def test_unknown_or_public_visibility_fails_closed(pages, repository, settings):
    with pytest.raises(ValueError):
        pages.private_site_url(repository, settings)


@pytest.mark.parametrize("url", [
    None, "", "http://example.pages.github.io/", "https:///missing-host",
    "https://user:password@example.com/", "https://example.com/?token=value",
    "https://example.com/#fragment", "https://example.com/\nsite_url=other",
])
def test_invalid_site_url_is_rejected(pages, url):
    with pytest.raises(ValueError):
        pages.private_site_url({"private": True, "visibility": "private"}, dict(site(), html_url=url))


def test_url_change_between_build_and_deploy_blocks_publication(pages):
    with pytest.raises(ValueError, match="changed"):
        pages.private_site_url(
            {"private": True, "visibility": "private"}, site(), "https://old.example.com/",
        )


@pytest.mark.parametrize("status", [200, 403, 404])
def test_cli_reads_existing_settings_without_creating_or_changing_site(
        pages, monkeypatch, tmp_path, capsys, status):
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(sys, "argv", ["pages.py"])
    calls = []

    def api(command, **kwargs):
        calls.append(command)
        if command[-1].endswith("/pages"):
            if status != 200:
                raise subprocess.CalledProcessError(1, command, stderr=f"gh: Error (HTTP {status})")
            return json.dumps(site())
        return json.dumps({"private": True, "visibility": "private"})

    monkeypatch.setattr(pages.subprocess, "check_output", api)
    assert pages.main() == (0 if status == 200 else 1)
    assert calls == [
        ["gh", "api", "repos/owner/repository"],
        ["gh", "api", "repos/owner/repository/pages"],
    ]
    if status == 200:
        assert output.read_text() == "site_url=https://example.pages.github.io/\n"
        assert not summary.exists()
    else:
        assert not output.exists()
        error = capsys.readouterr().err
        assert "::error title=Private Pages publication blocked::" in error
        assert "https://github.com/owner/repository/settings/pages" in error
        assert "Source = GitHub Actions and Visibility = Private" in error
        assert "pages: read" in error
        assert "returned non-zero exit status" not in error
        assert "No Pages site was created or made public." in summary.read_text()
        if status == 404:
            assert "HTTP 404: the Pages site is missing or inaccessible" in error
