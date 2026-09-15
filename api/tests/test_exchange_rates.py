import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from services import exchange_rates


XML = """<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube><Cube time="2026-08-13">
    <Cube currency="USD" rate="1.20"/>
    <Cube currency="GBP" rate="0.80"/>
    <Cube currency="INR" rate="100.00"/>
  </Cube></Cube>
</Envelope>
"""


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.request = httpx.Request("GET", exchange_rates.ECB_DAILY_RATES_URL)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=self.request, response=self)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def get(self, *args, **kwargs):
        return self.responses.pop(0)


def test_derives_cross_rates_from_eur_reference_table(monkeypatch):
    monkeypatch.setattr(exchange_rates, "_cached_table", None)
    now = datetime(2026, 8, 14, tzinfo=timezone.utc)

    result = asyncio.run(
        exchange_rates.get_exchange_rates("USD", client=FakeClient([FakeResponse(XML)]), now=now)
    )

    assert result["baseCurrency"] == "USD"
    assert result["publishedDate"] == "2026-08-13"
    assert result["rates"]["USD"] == 1.0
    assert result["rates"]["EUR"] == pytest.approx(1 / 1.2)
    assert result["rates"]["GBP"] == pytest.approx(0.8 / 1.2)
    assert result["rates"]["INR"] == pytest.approx(100 / 1.2)
    assert result["stale"] is False


def test_uses_recent_cached_table_when_ecb_is_unavailable(monkeypatch):
    fetched_at = datetime(2026, 8, 13, tzinfo=timezone.utc)
    monkeypatch.setattr(
        exchange_rates,
        "_cached_table",
        exchange_rates._parse_rate_table(XML, fetched_at),
    )

    async def no_wait(*args):
        return None

    monkeypatch.setattr(exchange_rates.asyncio, "sleep", no_wait)
    failed = [FakeResponse("", 503), FakeResponse("", 503), FakeResponse("", 503)]
    result = asyncio.run(
        exchange_rates.get_exchange_rates(
            "USD",
            client=FakeClient(failed),
            now=fetched_at + timedelta(days=1),
        )
    )

    assert result["stale"] is True
    assert result["publishedDate"] == "2026-08-13"


def test_rejects_currency_not_published_by_ecb(monkeypatch):
    monkeypatch.setattr(exchange_rates, "_cached_table", None)

    with pytest.raises(exchange_rates.UnsupportedCurrencyError):
        asyncio.run(
            exchange_rates.get_exchange_rates(
                "XYZ",
                client=FakeClient([FakeResponse(XML)]),
                now=datetime(2026, 8, 14, tzinfo=timezone.utc),
            )
        )