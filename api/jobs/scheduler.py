"""One bounded scheduler tick; invoked by the configured Container Apps Job.

Continues refresh cycles started from the Schedule tab's Export action, starts a schedule's
monthly close when it is due (pulling only closed months without a finalized copy), and runs
each schedule's daily pull of the current month through yesterday.
"""

import asyncio
import logging

from fastapi import HTTPException

from services import focus_schedules

logger = logging.getLogger(__name__)

_REPORT_REFRESH_STALE_DAYS = 90


async def run_once() -> int:
    subscription_ids = await asyncio.to_thread(focus_schedules.scheduled_subscription_ids)
    failures = 0
    new_data = False
    for offset in range(0, len(subscription_ids), 4):
        batch = subscription_ids[offset:offset + 4]
        # Both lanes take the schedule's lease, so each subscription's lanes run one after the other.
        for lane in (focus_schedules.advance, focus_schedules.advance_daily):
            results = await asyncio.gather(*(lane(value) for value in batch), return_exceptions=True)
            for subscription_id, result in zip(batch, results):
                if isinstance(result, HTTPException) and result.status_code in (404, 409):
                    continue
                if isinstance(result, BaseException):
                    failures += 1
                    logger.warning("FOCUS schedule %s could not advance; prerequisites or service availability need verification", subscription_id)
                    continue
                if isinstance(result, dict) and result.get("status") == "succeeded":
                    new_data = True
    if new_data:
        await _refresh_report_for_active_schedules()
    return failures


async def _refresh_report_for_active_schedules() -> None:
    # Local import: avoids pulling FastAPI app/module setup into every scheduler tick
    # that doesn't need it, and keeps a report-build failure from ever affecting the
    # scheduler's own failure count / exit code.
    try:
        subscription_ids = await focus_schedules.active_scheduled_subscription_ids()
        if not subscription_ids:
            return
        from main import _build_and_publish_report

        await _build_and_publish_report(subscription_ids, _REPORT_REFRESH_STALE_DAYS)
    except Exception:
        logger.warning("Automatic report refresh after a completed refresh cycle failed", exc_info=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(1 if asyncio.run(run_once()) else 0)