"""ECB EUR/USD exchange rate service for German tax calculations.

The German tax authorities (Finanzamt) accept the ECB daily reference rate
(Referenzkurs) as the authoritative EUR/USD rate for crypto tax calculations.
Published daily at ~16:00 CET. Weekends/holidays use the previous business day's rate.

Source: https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

ECB_API_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"


class ECBRateError(Exception):
    """Raised when ECB rate cannot be fetched or parsed."""


class ECBRateService:
    """Fetches and caches daily ECB EUR/USD reference rates.

    The rate returned is EUR per 1 USD (e.g., 0.92 means 1 USD = 0.92 EUR).
    For tax calculations: USD_amount / rate = EUR_amount
    (because the ECB quotes USD price of 1 EUR, we invert).
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._client = http_client or httpx.Client(timeout=30.0)
        self._cache: dict[date, Decimal] = {}

    def get_rate(self, for_date: date) -> Decimal:
        """Get EUR/USD rate for a given date. Returns USD per 1 EUR.

        If the date is a weekend/holiday, walks backward to find the most recent
        business day rate (ECB only publishes on business days).

        Returns:
            Decimal: USD per 1 EUR (e.g., 1.08 means 1 EUR = 1.08 USD).
                     To convert USD to EUR: usd_amount / rate
        """
        if for_date in self._cache:
            return self._cache[for_date]

        # Walk back up to 5 days to find the most recent business day
        check_date = for_date
        for _ in range(5):
            if check_date in self._cache:
                self._cache[for_date] = self._cache[check_date]
                return self._cache[check_date]
            check_date -= timedelta(days=1)

        # Fetch from ECB API
        rate = self._fetch_rate(for_date)
        self._cache[for_date] = rate
        return rate

    def usd_to_eur(self, usd_amount: Decimal, for_date: date) -> Decimal:
        """Convert USD to EUR using the ECB rate for the given date."""
        rate = self.get_rate(for_date)
        return usd_amount / rate

    def preload_range(self, start: date, end: date) -> None:
        """Preload rates for a date range (useful at startup for backfilling)."""
        self._fetch_range(start, end)

    def _fetch_rate(self, for_date: date) -> Decimal:
        """Fetch a single day's rate from the ECB API."""
        # Fetch a small window to handle weekends/holidays
        start = for_date - timedelta(days=5)
        rates = self._fetch_range(start, for_date)
        if not rates:
            raise ECBRateError(f"No ECB rate available for {for_date} or preceding 5 days")

        # Return the most recent rate on or before for_date
        for d in sorted(rates.keys(), reverse=True):
            if d <= for_date:
                return rates[d]
        raise ECBRateError(f"No ECB rate found on or before {for_date}")

    def _fetch_range(self, start: date, end: date) -> dict[date, Decimal]:
        """Fetch rates from ECB SDMX API for a date range."""
        params = {
            "startPeriod": start.isoformat(),
            "endPeriod": end.isoformat(),
            "format": "csvdata",
        }
        try:
            resp = self._client.get(ECB_API_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ECBRateError(f"ECB API request failed: {e}") from e

        rates = self._parse_csv(resp.text)
        self._cache.update(rates)
        return rates

    @staticmethod
    def _parse_csv(csv_text: str) -> dict[date, Decimal]:
        """Parse ECB SDMX CSV response into date→rate mapping."""
        rates: dict[date, Decimal] = {}
        lines = csv_text.strip().split("\n")
        if len(lines) < 2:
            return rates

        header = lines[0].split(",")
        try:
            date_idx = header.index("TIME_PERIOD")
            value_idx = header.index("OBS_VALUE")
        except ValueError as e:
            raise ECBRateError(f"Unexpected CSV header: {header}") from e

        for line in lines[1:]:
            cols = line.split(",")
            if len(cols) <= max(date_idx, value_idx):
                continue
            try:
                rate_date = date.fromisoformat(cols[date_idx])
                rate_value = Decimal(cols[value_idx])
                rates[rate_date] = rate_value
            except (ValueError, InvalidOperation):
                continue

        return rates

    def close(self) -> None:
        self._client.close()
