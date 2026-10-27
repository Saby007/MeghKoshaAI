import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.responses import JSONResponse

from services import arm_client, azure_request_pacing, focus_history_reader
from services.assessment_http import AssessmentHTTPBudget
from services.assessment_limits import AssessmentBudget, AssessmentLimitError, active_budget
from services.assessment_request_context import dashboard_read
from services.assessment_settings import settings
from services.history_records import HistoryRecords, HistorySegment
from reports.builder import build_full_report
from test_focus_history_reader import _content, _run
from test_scalable_assessments import SUB, make_report


@pytest.fixture(autouse=True)
def clear_settings():
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_disk_history_replays_calendar_tags_and_report_results_without_raw_lists(monkeypatch, tmp_path):
    files = {}
    selected = []
    for period in ("2026-06", "2026-07"):
        path = tmp_path / f"{period}.csv.gz"
        path.write_bytes(_content(SUB, period))
        files[period] = {f"focus/{period}/part.csv.gz": path}
        selected.append((period, {SUB: _run(period, period)}, period))
    monkeypatch.setattr(focus_history_reader.focus_cost_reader, "_download_run_files",
                        lambda ids, runs, period: files[period])
    groups = {(SUB, "rg"): {"application": "synthetic"}}
    memory = focus_history_reader._parse_history_files([SUB], selected, groups)
    budget = AssessmentBudget(settings(), tmp_path)
    token = active_budget.set(budget)
    try:
        disk = focus_history_reader._parse_history_files([SUB], selected, groups)
        assert isinstance(disk.records, HistoryRecords)
        assert not isinstance(disk.records, list)
        assert len(disk.records) == 61
        assert disk.records == memory.records
        assert list(disk.records[29:33]) == memory.records[29:33]
        assert disk.records[-1] == memory.records[-1]
        assert list(disk.records[::-1]) == list(reversed(memory.records))
        assert list(disk.records) == list(disk.records)
        base = make_report()
        arguments = dict(
            subscription_ids=[SUB], subscription_names={SUB: "Synthetic"}, per_sub_spend={SUB: 31},
            resource_graph_rows={}, cost_by_resource_id={}, service_family_spend={},
            advisor_recommendations=[], untagged_counts={}, report_metadata=base.report_metadata,
            completeness=base.completeness,
        )
        expected = build_full_report(**arguments, focus_history=memory)
        actual = build_full_report(**arguments, focus_history=disk)
        assert actual.model_dump() == expected.model_dump()
        assert actual.cost_details.status == "complete"
        assert budget.used["history_rows"] == 61
    finally:
        active_budget.reset(token)


def test_history_spool_limit_fails_before_buffering_or_writing(tmp_path):
    from anomalies.models import DailyCostRecord
    segment = HistorySegment(AssessmentBudget(replace(settings(), spool_bytes=5), tmp_path))
    with pytest.raises(AssessmentLimitError, match="spool_bytes"):
        segment.append(DailyCostRecord("2026-07-01", SUB, "Sub", "VM", "rg", "resource", "vm", 1.0))
    assert len(segment) == 0
    assert segment.pending_bytes == 0
    assert not segment.path.exists()


def test_worker_rate_scopes_are_shared_by_identity_or_subscription_not_process(monkeypatch):
    monkeypatch.setattr(azure_request_pacing.runtime_identity, "client_id", lambda: "synthetic-mi")
    graph, interval = azure_request_pacing.scope("/providers/Microsoft.ResourceGraph/resources?api-version=1")
    assert graph == "synthetic-mi|resource-graph|identity"
    assert interval == 0.4
    first, _ = azure_request_pacing.scope(f"/subscriptions/{SUB}/providers/Microsoft.Insights/metrics")
    second, _ = azure_request_pacing.scope("/subscriptions/other/providers/Microsoft.Insights/metrics")
    assert first != second
    assert "|metrics|" in first
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setenv("ASSESSMENT_DB_USER", "meghkosha_worker")
    assert azure_request_pacing.enabled()
    monkeypatch.setenv("ASSESSMENT_DB_USER", "meghkosha_api")
    assert not azure_request_pacing.enabled()


def test_retry_delay_honours_provider_headers_and_resource_graph_reset():
    assert azure_request_pacing.retry_delay(httpx.Headers({
        "Retry-After": "2", "x-ms-user-quota-resets-after": "00:00:05",
        "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after": "12",
    }), 0) == 12
    assert azure_request_pacing.retry_delay(httpx.Headers({"Retry-After": "NaN"}), 2) == 4
    assert azure_request_pacing.retry_delay(httpx.Headers({"Retry-After": "invalid"}), 1) == 2


@pytest.mark.parametrize("method, expected_calls", [("GET", 2), ("POST", 1)])
def test_worker_retries_safe_reads_but_never_blindly_replays_mutating_post(monkeypatch, method, expected_calls):
    responses = []

    def transport(request):
        responses.append(request)
        if len(responses) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, request=request)
        return httpx.Response(200, json={"value": []}, request=request)

    client = httpx.AsyncClient
    monkeypatch.setattr(arm_client.httpx, "AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setattr(arm_client, "_token", AsyncMock(return_value="synthetic"))
    monkeypatch.setattr(azure_request_pacing, "enabled", lambda: True)
    wait = AsyncMock()
    defer = AsyncMock()
    monkeypatch.setattr(azure_request_pacing, "wait", wait)
    monkeypatch.setattr(azure_request_pacing, "defer", defer)
    monkeypatch.setattr(arm_client.asyncio, "sleep", AsyncMock())

    async def exercise():
        if method == "POST":
            with pytest.raises(httpx.HTTPStatusError):
                await arm_client._arm_request(method, f"/subscriptions/{SUB}/provider/test")
        else:
            assert await arm_client._arm_request(method, f"/subscriptions/{SUB}/provider/test") == {"value": []}
    asyncio.run(exercise())
    assert len(responses) == expected_calls
    assert wait.await_count == expected_calls
    assert defer.await_args.args[1] == 2


def test_dashboard_reads_keep_capacity_while_work_admission_is_occupied(monkeypatch):
    monkeypatch.setenv("ASSESSMENT_MODE", "postgres")
    monkeypatch.setenv("ASSESSMENT_WORK_CONCURRENCY", "1")
    monkeypatch.setenv("ASSESSMENT_API_CONCURRENCY", "2")

    async def exercise():
        occupied = asyncio.Event()
        release = asyncio.Event()

        async def service(scope, receive, send):
            if scope["method"] == "POST":
                assert not dashboard_read.get()
                occupied.set()
                await release.wait()
            else:
                assert dashboard_read.get()
            await JSONResponse({"ok": True})(scope, receive, send)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=AssessmentHTTPBudget(service)),
                                    base_url="http://test") as client:
            work = asyncio.create_task(client.post("/api/report", json={}))
            try:
                await asyncio.wait_for(occupied.wait(), 2)
                assert (await client.get("/api/report/latest")).status_code == 200
                assert (await client.post("/api/report", json={})).status_code == 429
            finally:
                release.set()
                assert (await work).status_code == 200
    asyncio.run(exercise())


@pytest.mark.parametrize("script", ["deploy-ai.ps1"])
def test_legacy_helpers_refuse_default_postgres_rollout_before_any_azure_command(script):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required")
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(root / "scripts" / script), "-EnvironmentName", "offline-only"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "manual-scalable-deployment.md" in result.stderr


def test_default_end_to_end_command_plans_every_private_stage_without_cloud_execution():
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required")
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(root / "scripts" / "deploy-end-to-end.ps1"),
         "-EnvironmentName", "offline-only", "-PlanOnly"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["stage"] == "All"
    assert plan["privateRegistry"] and plan["privateDatabase"]
    assert not plan["appliesChanges"]
    assert plan["stages"] == [
        "Initialize", "Foundation", "Build", "Bootstrap", "Identity", "Permissions", "Release", "Verify", "StopBuilder",
    ]


def test_manual_runbook_powershell_blocks_parse_without_executing_them(tmp_path):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required")
    root = Path(__file__).parents[2]
    document = (root / "docs" / "manual-scalable-deployment.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```powershell\n(.*?)```", document, re.S)
    assert len(blocks) >= 10
    payload = tmp_path / "blocks.json"
    payload.write_text(json.dumps(blocks), encoding="utf-8")
    environment = dict(os.environ, MANUAL_BLOCKS=str(payload))
    command = """
$ErrorActionPreference='Stop'
$blocks=Get-Content -LiteralPath $env:MANUAL_BLOCKS -Raw | ConvertFrom-Json
foreach($block in $blocks) {
    $tokens=$null; $errors=$null
    [void][System.Management.Automation.Language.Parser]::ParseInput($block,[ref]$tokens,[ref]$errors)
    if($errors.Count) { throw ($errors | Out-String) }
}
"""
    result = subprocess.run([shell, "-NoProfile", "-Command", command], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
