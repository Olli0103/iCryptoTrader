"""Tests for ECB EUR/USD rate service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest

from icryptotrader.tax.ecb_rates import ECBRateError, ECBRateService

# Sample CSV response from ECB API
SAMPLE_CSV = """DATAFLOW,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE
EXR,D,USD,EUR,SP00,A,2025-01-06,1.0384
EXR,D,USD,EUR,SP00,A,2025-01-07,1.0346
EXR,D,USD,EUR,SP00,A,2025-01-08,1.0327
EXR,D,USD,EUR,SP00,A,2025-01-09,1.0302
EXR,D,USD,EUR,SP00,A,2025-01-10,1.0249"""

# Weekend: Jan 11 (Sat), Jan 12 (Sun) — no ECB rates published


def _mock_client(csv_response: str = SAMPLE_CSV) -> httpx.Client:
    """Create a mock httpx.Client that returns the given CSV."""
    mock = MagicMock(spec=httpx.Client)
    resp = MagicMock(spec=httpx.Response)
    resp.text = csv_response
    resp.raise_for_status = MagicMock()
    mock.get.return_value = resp
    return mock


class TestCSVParsing:
    def test_parse_sample_csv(self) -> None:
        rates = ECBRateService._parse_csv(SAMPLE_CSV)
        assert len(rates) == 5
        assert rates[date(2025, 1, 6)] == Decimal("1.0384")
        assert rates[date(2025, 1, 10)] == Decimal("1.0249")

    def test_empty_csv(self) -> None:
        rates = ECBRateService._parse_csv("")
        assert rates == {}

    def test_header_only(self) -> None:
        rates = ECBRateService._parse_csv(
            "DATAFLOW,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
        )
        assert rates == {}

    def test_bad_header_raises(self) -> None:
        with pytest.raises(ECBRateError, match="Unexpected CSV header"):
            ECBRateService._parse_csv("FOO,BAR\n1,2")

    def test_malformed_value_skipped(self) -> None:
        csv = (
            "DATAFLOW,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
            "EXR,D,USD,EUR,SP00,A,2025-01-06,1.0384\n"
            "EXR,D,USD,EUR,SP00,A,bad-date,1.0346\n"
            "EXR,D,USD,EUR,SP00,A,2025-01-08,not_a_number\n"
        )
        rates = ECBRateService._parse_csv(csv)
        assert len(rates) == 1
        assert date(2025, 1, 6) in rates


class TestGetRate:
    def test_fetches_weekday_rate(self) -> None:
        svc = ECBRateService(http_client=_mock_client())
        rate = svc.get_rate(date(2025, 1, 8))
        assert rate == Decimal("1.0327")

    def test_weekend_falls_back_to_friday(self) -> None:
        svc = ECBRateService(http_client=_mock_client())
        # Saturday Jan 11 — should get Friday Jan 10's rate
        rate = svc.get_rate(date(2025, 1, 11))
        assert rate == Decimal("1.0249")

    def test_caches_after_first_fetch(self) -> None:
        mock = _mock_client()
        svc = ECBRateService(http_client=mock)
        svc.get_rate(date(2025, 1, 8))
        svc.get_rate(date(2025, 1, 8))
        assert mock.get.call_count == 1

    def test_cache_serves_adjacent_dates(self) -> None:
        mock = _mock_client()
        svc = ECBRateService(http_client=mock)
        # First call fetches and caches the range
        svc.get_rate(date(2025, 1, 8))
        # Second call for a date in the same range should hit cache
        rate = svc.get_rate(date(2025, 1, 6))
        assert rate == Decimal("1.0384")
        assert mock.get.call_count == 1


class TestUsdToEur:
    def test_conversion(self) -> None:
        svc = ECBRateService(http_client=_mock_client())
        eur = svc.usd_to_eur(Decimal("1000"), date(2025, 1, 6))
        expected = Decimal("1000") / Decimal("1.0384")
        assert abs(eur - expected) < Decimal("0.01")


class TestAPIError:
    def test_http_error_raises(self) -> None:
        mock = MagicMock(spec=httpx.Client)
        mock.get.side_effect = httpx.ConnectError("Connection refused")
        svc = ECBRateService(http_client=mock)
        with pytest.raises(ECBRateError, match="ECB API request failed"):
            svc.get_rate(date(2025, 1, 8))
