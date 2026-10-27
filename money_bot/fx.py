"""PLN conversion with the official NBP table A mid rate."""

from datetime import date, timedelta
from decimal import Decimal

import requests

URL = "https://api.nbp.pl/api/exchangerates/rates/a/{code}/{start}/{end}/?format=json"
CENT = Decimal("0.01")


class Rates:
    def __init__(self, get=requests.get):
        self.get = get
        self.cache: dict[tuple[str, date], Decimal] = {}

    def rate(self, code: str, day: date) -> Decimal:
        """Mid rate in force on `day`: the last one published on or before it (none on weekends)."""
        if code == "PLN":
            return Decimal(1)
        key = (code, day)
        if key not in self.cache:
            url = URL.format(code=code.lower(), start=day - timedelta(days=10), end=day)
            response = self.get(url, timeout=30)
            if response.status_code == 404:
                raise LookupError(f"no NBP rate for {code} in the 10 days to {day}")
            response.raise_for_status()
            published = [r for r in response.json()["rates"] if r["effectiveDate"] <= day.isoformat()]
            if not published:
                raise LookupError(f"no NBP rate for {code} on or before {day}")
            # Today's rate before NBP publishes (~12:00) is yesterday's; the cache keeps that for the day.
            self.cache[key] = Decimal(str(published[-1]["mid"]))
        return self.cache[key]

    def to_pln(self, amount: Decimal, code: str, day: date) -> Decimal:
        return (amount * self.rate(code, day)).quantize(CENT)
