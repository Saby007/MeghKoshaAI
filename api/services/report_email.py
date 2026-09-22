"""Managed-identity delivery of authenticated links to persisted reports."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlencode
from uuid import uuid4

from azure.communication.email import EmailClient
from azure.storage.blob import ContentSettings

from brand import BRAND_NAME
from reports.models import ReportSnapshot
from services import runtime_identity
from services.report_snapshots import get_container_client

_EMAIL = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.IGNORECASE)
_REPORT_LABELS = {
    "executive": "Executive Summary",
    "full": "Full Assessment",
    "chargeback": "Chargeback",
    "compliance": "Compliance",
    "finops": "FinOps Monthly",
}


class ReportEmailConfigurationError(RuntimeError):
    pass


class ReportEmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportEmailDelivery:
    attempt_id: str
    operation_id: str
    recipient: str
    status: str


def normalize_recipient(value: str) -> str:
    recipient = value.strip().lower()
    if len(recipient) > 320 or "\r" in recipient or "\n" in recipient or not _EMAIL.fullmatch(recipient):
        raise ValueError("The authenticated identity does not contain a deliverable email address")
    return recipient


def _required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ReportEmailConfigurationError(f"{name} is not configured")
    return value


def _download_url(snapshot_id: str, report_type: str) -> str:
    public_url = _required_setting("REPORT_PUBLIC_APP_URL").rstrip("/")
    if not public_url.startswith("https://"):
        raise ReportEmailConfigurationError("REPORT_PUBLIC_APP_URL must use HTTPS")
    query = urlencode({"snapshotId": snapshot_id, "type": report_type})
    return f"{public_url}/api/report/export/snapshot?{query}"


def _message(snapshot: ReportSnapshot, report_type: str, recipient: str) -> dict:
    label = _REPORT_LABELS[report_type]
    metadata = snapshot.report.report_metadata
    download_url = _download_url(snapshot.snapshot_id, report_type)
    subject = f"{BRAND_NAME} {label} - {metadata.period}"
    safe_brand = html.escape(BRAND_NAME)
    safe_label = html.escape(label)
    safe_period = html.escape(metadata.period)
    safe_url = html.escape(download_url, quote=True)
    plain_text = (
        f"Your {label} report for {metadata.period} is ready.\n\n"
        f"Download the exact completed snapshot: {download_url}\n\n"
        "Microsoft Entra sign-in is required."
    )
    return {
        "senderAddress": _required_setting("EMAIL_SENDER_ADDRESS"),
        "recipients": {"to": [{"address": recipient}]},
        "content": {
            "subject": subject,
            "plainText": plain_text,
            "html": (
                f"<h1>{safe_brand} report ready</h1>"
                f"<p>Your <strong>{safe_label}</strong> report for {safe_period} is ready.</p>"
                f'<p><a href="{safe_url}">Download the exact completed snapshot</a></p>'
                "<p>Microsoft Entra sign-in is required. This link does not grant report access.</p>"
            ),
        },
    }


@lru_cache(maxsize=1)
def _client() -> EmailClient:
    return EmailClient(_required_setting("EMAIL_ENDPOINT"), runtime_identity.credential())


def _event_blob(scope_hash: str, attempt_id: str, status: str, created_at: str) -> str:
    day = created_at[:10]
    return f"email/{scope_hash}/events/{day}/{attempt_id}-{status.lower()}.json"


def _write_event(
    *,
    snapshot: ReportSnapshot,
    report_type: str,
    recipient: str,
    actor: str,
    attempt_id: str,
    status: str,
    operation_id: str = "",
    error: str = "",
) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    event = {
        "actor": actor,
        "attemptId": attempt_id,
        "createdAt": created_at,
        "error": error[:500],
        "operationId": operation_id,
        "recipient": recipient,
        "reportType": report_type,
        "scopeHash": snapshot.scope_hash,
        "snapshotId": snapshot.snapshot_id,
        "status": status,
    }
    payload = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    get_container_client().upload_blob(
        _event_blob(snapshot.scope_hash, attempt_id, status, created_at),
        payload,
        overwrite=False,
        content_settings=ContentSettings(content_type="application/json", cache_control="no-store"),
        metadata={"sha256": hashlib.sha256(payload).hexdigest()},
    )


def _send(message: dict) -> tuple[str, str]:
    result = _client().begin_send(message).result()
    operation_id = str(result.get("id") or "")
    status = str(result.get("status") or "Unknown")
    if status.lower() != "succeeded":
        raise ReportEmailDeliveryError(f"ACS Email returned status {status}")
    return operation_id, status


async def send_report_link(
    snapshot: ReportSnapshot,
    report_type: str,
    recipient_value: str,
    actor: str,
) -> ReportEmailDelivery:
    if report_type not in _REPORT_LABELS:
        raise ValueError("Unsupported report type")
    recipient = normalize_recipient(recipient_value)
    message = _message(snapshot, report_type, recipient)
    attempt_id = uuid4().hex
    await asyncio.to_thread(
        _write_event,
        snapshot=snapshot,
        report_type=report_type,
        recipient=recipient,
        actor=actor,
        attempt_id=attempt_id,
        status="Started",
    )
    try:
        operation_id, status = await asyncio.to_thread(_send, message)
    except Exception as error:
        await asyncio.to_thread(
            _write_event,
            snapshot=snapshot,
            report_type=report_type,
            recipient=recipient,
            actor=actor,
            attempt_id=attempt_id,
            status="Failed",
            error=str(error),
        )
        if isinstance(error, ReportEmailDeliveryError):
            raise
        raise ReportEmailDeliveryError("ACS Email delivery failed") from error
    await asyncio.to_thread(
        _write_event,
        snapshot=snapshot,
        report_type=report_type,
        recipient=recipient,
        actor=actor,
        attempt_id=attempt_id,
        status=status,
        operation_id=operation_id,
    )
    return ReportEmailDelivery(attempt_id, operation_id, recipient, status)