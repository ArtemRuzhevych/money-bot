"""monobank personal API (read-only: it has no payment endpoints) and its rows for the Sheet."""

from datetime import datetime
from decimal import Decimal

import requests

from money_bot.handlers import TZ

BASE = "https://api.monobank.ua"
CURRENCIES = {980: "UAH", 985: "PLN", 840: "USD", 978: "EUR", 826: "GBP"}  # ISO 4217 numeric


class Monobank:
    def __init__(self, token: str, session=None):
        self.token = token
        self.session = session or requests.Session()

    def _get(self, path: str):
        response = self.session.get(BASE + path, headers={"X-Token": self.token}, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"monobank {path}: HTTP {response.status_code} {response.text[:200]}")
        return response.json()

    def card_accounts(self) -> list[dict]:
        return self._get("/personal/client-info")["accounts"]  # jars come in a separate "jars" list

    def statement(self, account_id: str, start: int, end: int) -> list[dict]:
        return self._get(f"/personal/statement/{account_id}/{start}/{end}")


def currency(code: int) -> str:
    return CURRENCIES.get(code, str(code))


def to_row(item: dict, account: dict, rates) -> dict:
    when = datetime.fromtimestamp(item["time"], TZ)
    code = currency(account["currencyCode"])
    amount = Decimal(item["amount"]) / 100  # minor units in the account currency
    description = item.get("description", "")
    note = ""
    if item.get("currencyCode", account["currencyCode"]) != account["currencyCode"]:
        note = f"operation: {Decimal(item['operationAmount']) / 100:.2f} {currency(item['currencyCode'])}"
    pan = (account.get("maskedPan") or ["????"])[0][-4:]
    return {
        "id": f"mono-{item['id']}",
        "date": when.strftime("%Y-%m-%d %H:%M"),
        "amount": float(amount),
        "currency": code,
        "amount_pln": float(rates.to_pln(amount, code, when.date())),
        "account": f"mono {code} *{pan}",
        "merchant": description,
        "description": f"{description}: {item['comment']}" if item.get("comment") else description,
        "source": "api",
        "note": note,
    }
