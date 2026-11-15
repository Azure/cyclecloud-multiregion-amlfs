# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

import pytest

from tests.test_project_install import load_script


@pytest.fixture
def report():
    return load_script("report", directory="ci")


def outcomes(report, **changes):
    values = {key: "success" for key, _ in report.PHASES}
    values.update(scenario="success", cleanup="skipped")
    values.update(changes)
    return values


def test_success_report_contains_only_generic_results(report):
    text, valid = report.render_report(True, outcomes(report))
    assert valid
    assert text.count("2/2 jobs; input and output verified") == 4
    assert "| Overall | ✅ Passed |" in text
    assert "| Cleanup | ⏭️ Not run |" in text


def test_failure_never_claims_regional_verification_or_exposes_diagnostics(report):
    values = outcomes(report, workloads="failure", scenario="failure")
    values.update(
        error="/subscriptions/private-sub/resourceGroups/private-group/providers/type/name",
        resource_group="private-group", subscription_id="private-sub",
        hostname="private-host", ip="10.0.0.4", tenant_id="private-tenant", job_id="12345",
    )
    text, valid = report.render_report(True, values)
    assert valid
    assert "| Overall | ❌ Failed |" in text
    assert "2/2" not in text
    for sensitive in ("subscriptions", "private-", "10.0.0.4", "12345"):
        assert sensitive not in text


@pytest.mark.parametrize("injected", [
    "/subscriptions/private-sub/resourceGroups/private-group",
    "storage-account-name", "10.0.0.4", {"error": "private-details"},
])
def test_unexpected_statuses_are_not_reflected_in_summary(report, injected):
    text, valid = report.render_report(True, outcomes(report, workspace=injected))
    assert not valid
    assert "| Overall | ⚪ Incomplete |" in text
    assert "private" not in text and "storage-account" not in text and "10.0.0.4" not in text


def test_missing_phase_cannot_produce_successful_deployment_report(report):
    text, valid = report.render_report(True, outcomes(report, installation=""))
    assert not valid
    assert "| Overall | ⚪ Incomplete |" in text


def test_cleanup_only_report_does_not_claim_e2e_success(report):
    text, valid = report.render_report(False, {"scenario": "skipped", "cleanup": "success"})
    assert valid
    assert "Cleanup report" in text
    assert "Region" not in text and "E2E" not in text
    assert "| Cleanup | ✅ Passed |" in text


@pytest.mark.parametrize("cleanup,scenario,overall", [
    ("success", "success", "✅ Passed"),
    ("skipped", "success", "✅ Passed"),
    ("failure", "failure", "❌ Failed"),
    ("cancelled", "cancelled", "⚪ Cancelled"),
])
def test_inline_cleanup_preserves_workload_results_and_overall_failure(
    report, cleanup, scenario, overall,
):
    text, valid = report.render_report(True, outcomes(
        report, cleanup=cleanup, scenario=scenario,
    ))
    assert valid
    assert text.count("2/2 jobs; input and output verified") == 4
    assert f"| Overall | {overall} |" in text


@pytest.mark.parametrize("cleanup", ["success", "failure", "cancelled"])
def test_inline_cleanup_only_reports_job_outcome_without_deployment_claims(report, cleanup):
    text, valid = report.render_report(False, {"scenario": cleanup, "cleanup": cleanup})
    assert valid
    assert "Region" not in text and "E2E" not in text
    assert f"| Overall | {report.LABELS[cleanup]} |" in text


def test_main_writes_only_sanitized_table(report, tmp_path, monkeypatch, capsys):
    path = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    monkeypatch.setenv("REPORT_DEPLOY", "true")
    for key, value in outcomes(report).items():
        monkeypatch.setenv("RESULT_" + key.upper(), value)
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "private-subscription")
    monkeypatch.setenv("FOUR_REGION_CONFIG", '{"resource_id": "private-resource"}')
    assert report.main() == 0
    text = path.read_text()
    assert capsys.readouterr().out == text
    assert "private" not in text
