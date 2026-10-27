from datetime import date
from decimal import Decimal

import pytest

from money_bot.fx import Rates


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def nbp(*pairs):
    return FakeResponse(200, {"rates": [{"effectiveDate": d, "mid": m} for d, m in pairs]})


def fake_get(*responses):
    calls = []

    def get(url, timeout):
        calls.append(url)
        return responses[len(calls) - 1]

    return get, calls


def test_weekend_uses_last_rate_before_the_day():
    get, calls = fake_get(nbp(("2026-10-22", 0.0863), ("2026-10-23", 0.0868)))
    rates = Rates(get)

    assert rates.rate("UAH", date(2026, 10, 25)) == Decimal("0.0868")
    assert calls == ["https://api.nbp.pl/api/exchangerates/rates/a/uah/2026-10-15/2026-10-25/?format=json"]


def test_ignores_rates_after_the_day_and_caches():
    get, calls = fake_get(nbp(("2026-10-23", 0.0868), ("2026-10-26", 0.0862)))
    rates = Rates(get)

    assert rates.rate("UAH", date(2026, 10, 24)) == Decimal("0.0868")
    assert rates.rate("UAH", date(2026, 10, 24)) == Decimal("0.0868")
    assert len(calls) == 1


def test_pln_needs_no_lookup():
    get, calls = fake_get()
    assert Rates(get).to_pln(Decimal("12.34"), "PLN", date(2026, 10, 26)) == Decimal("12.34")
    assert calls == []


def test_to_pln_rounds_to_cents():
    get, _ = fake_get(nbp(("2026-10-26", 0.0862)))
    assert Rates(get).to_pln(Decimal("-245.00"), "UAH", date(2026, 10, 26)) == Decimal("-21.12")


def test_missing_rate_raises():
    get, _ = fake_get(FakeResponse(404))
    with pytest.raises(LookupError):
        Rates(get).rate("UAH", date(2026, 10, 25))
