import asyncio

import pytest

from services import report_email
from services import report_snapshots
from tests.test_report_exports import _snapshot


def test_normalizes_only_deliverable_authenticated_email():
    assert report_email.normalize_recipient(" SSAMADDA@Microsoft.com ") == "ssamadda@microsoft.com"
    with pytest.raises(ValueError):
        report_email.normalize_recipient("ssamadda@microsoft.com\r\nBcc:external@example.com")
    with pytest.raises(ValueError):
        report_email.normalize_recipient("not-an-email")


def test_message_uses_exact_authenticated_snapshot_link(monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER_ADDRESS", "DoNotReply@example.azurecomm.net")
    monkeypatch.setenv("REPORT_PUBLIC_APP_URL", "https://app.example")
    snapshot = _snapshot()

    message = report_email._message(snapshot, "executive", "ssamadda@microsoft.com")

    assert message["recipients"] == {"to": [{"address": "ssamadda@microsoft.com"}]}
    assert f"snapshotId={snapshot.snapshot_id}" in message["content"]["plainText"]
    assert "type=executive" in message["content"]["plainText"]
    assert message["senderAddress"] == "DoNotReply@example.azurecomm.net"


def test_send_writes_started_then_succeeded_audit(monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER_ADDRESS", "DoNotReply@example.azurecomm.net")
    monkeypatch.setenv("REPORT_PUBLIC_APP_URL", "https://app.example")
    snapshot = _snapshot()
    events = []

    monkeypatch.setattr(report_email, "_write_event", lambda **values: events.append(values))
    monkeypatch.setattr(report_email, "_send", lambda message: ("operation-1", "Succeeded"))

    result = asyncio.run(report_email.send_report_link(
        snapshot, "executive", "ssamadda@microsoft.com", "tenant:user"
    ))

    assert [event["status"] for event in events] == ["Started", "Succeeded"]
    assert events[1]["operation_id"] == "operation-1"
    assert result.recipient == "ssamadda@microsoft.com"
    assert result.status == "Succeeded"


def test_send_records_failure(monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER_ADDRESS", "DoNotReply@example.azurecomm.net")
    monkeypatch.setenv("REPORT_PUBLIC_APP_URL", "https://app.example")
    events = []
    monkeypatch.setattr(report_email, "_write_event", lambda **values: events.append(values))
    monkeypatch.setattr(
        report_email,
        "_send",
        lambda message: (_ for _ in ()).throw(report_email.ReportEmailDeliveryError("denied")),
    )

    with pytest.raises(report_email.ReportEmailDeliveryError, match="denied"):
        asyncio.run(report_email.send_report_link(
            _snapshot(), "full", "ssamadda@microsoft.com", "tenant:user"
        ))

    assert [event["status"] for event in events] == ["Started", "Failed"]