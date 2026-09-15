"""Indicative currency conversion using the ECB's daily EUR reference table."""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

ECB_DAILY_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_PROVIDER_URL = (
    "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/"
    "euro_reference_exchange_rates/html/index.en.html"
)
FRESH_TTL = timedelta(hours=12)
MAX_STALE_AGE = timedelta(days=7)


class ExchangeRateUnavailableError(RuntimeError):
    pass


class UnsupportedCurrencyError(ValueError):
    pass


@dataclass(frozen=True)
class RateTable:
    published_date: str
    rates_per_eur: dict[str, float]
    fetched_at: datetime


_cached_table: RateTable | None = None


def _parse_rate_table(xml: str, fetched_at: datetime) -> RateTable:
    root = ElementTree.fromstring(xml)
    dated_cube = root.find(".//{*}Cube[@time]")
    if dated_cube is None or not dated_cube.get("time"):
        raise ValueError("ECB response does not contain a publication date")

    rates = {"EUR": 1.0}
    for cube in dated_cube.findall("{*}Cube"):
        currency = str(cube.get("currency") or "").upper()
        raw_rate = cube.get("rate")
        if len(currency) != 3 or raw_rate is None:
            continue
        rate = float(raw_rate)
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError(f"ECB returned an invalid {currency} rate")
        rates[currency] = rate

    if len(rates) < 2:
        raise ValueError("ECB response does not contain currency rates")
    return RateTable(
        published_date=str(dated_cube.get("time")),
        rates_per_eur=rates,
        fetched_at=fetched_at,
    )


async def _download_rate_table(client: httpx.AsyncClient, now: datetime) -> RateTable:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.get(
                ECB_DAILY_RATES_URL,
                headers={"Accept": "application/xml", "User-Agent": "MeghKoshaAI/1.0"},
            )
            response.raise_for_status()
            return _parse_rate_table(response.text, now)
        except (httpx.HTTPError, ElementTree.ParseError, ValueError) as error:
            last_error = error
            if attempt < 2:
                await asyncio.sleep(2**attempt)

    raise ExchangeRateUnavailableError("ECB reference rates are unavailable") from last_error


def _cross_rates(table: RateTable, base_currency: str) -> dict[str, float]:
    base = base_currency.upper()
    base_rate = table.rates_per_eur.get(base)
    if base_rate is None:
        raise UnsupportedCurrencyError(f"ECB does not publish a reference rate for {base}")
    return {
        currency: round(rate / base_rate, 8)
        for currency, rate in sorted(table.rates_per_eur.items())
    }


def _response(table: RateTable, base_currency: str, stale: bool) -> dict:
    return {
        "baseCurrency": base_currency.upper(),
        "provider": "European Central Bank",
        "providerUrl": ECB_PROVIDER_URL,
        "publishedDate": table.published_date,
        "fetchedAt": table.fetched_at.isoformat(),
        "stale": stale,
        "rates": _cross_rates(table, base_currency),
        "disclaimer": "Indicative ECB reference rates; not for transaction settlement.",
    }


async def get_exchange_rates(
    base_currency: str,
    *,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> dict:
    global _cached_table

    current_time = now or datetime.now(timezone.utc)
    if _cached_table and current_time - _cached_table.fetched_at <= FRESH_TTL:
        return _response(_cached_table, base_currency, stale=False)

    try:
        if client is not None:
            table = await _download_rate_table(client, current_time)
        else:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http_client:
                table = await _download_rate_table(http_client, current_time)
        _cached_table = table
        return _response(table, base_currency, stale=False)
    except ExchangeRateUnavailableError:
        if _cached_table and current_time - _cached_table.fetched_at <= MAX_STALE_AGE:
            logger.warning("Using stale ECB reference rates from %s", _cached_table.published_date)
            return _response(_cached_table, base_currency, stale=True)
        raise