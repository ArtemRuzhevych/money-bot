from datetime import datetime
from decimal import Decimal

import pytest

from money_bot.handlers import TZ
from money_bot.monobank import Monobank, to_row

ACCOUNT = {"id": "acc1", "currencyCode": 980, "maskedPan": ["537541******1234"], "type": "black"}


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self.data = data
        self.text = str(data)

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers))
        return self.response


def test_card_accounts_ignores_jars():
    session = FakeSession(FakeResponse(200, {"accounts": [ACCOUNT], "jars": [{"id": "jar1"}]}))
    assert Monobank("tok", session).card_accounts() == [ACCOUNT]
    assert session.calls == [("https://api.monobank.ua/personal/client-info", {"X-Token": "tok"})]


def test_statement_path():
    session = FakeSession(FakeResponse(200, []))
    Monobank("tok", session).statement("acc1", 100, 200)
    assert session.calls[0][0] == "https://api.monobank.ua/personal/statement/acc1/100/200"


def test_http_error_raises():
    session = FakeSession(FakeResponse(429, {"errorDescription": "Too many requests"}))
    with pytest.raises(RuntimeError, match="429"):
        Monobank("tok", session).statement("acc1", 100, 200)


class FakeRates:
    def to_pln(self, amount, code, day):
        assert code == "UAH"
        return (amount * Decimal("0.0862")).quantize(Decimal("0.01"))


def test_to_row_maps_a_card_payment():
    item = {
        "id": "ZuHWzqkKGVo=",
        "time": int(datetime(2026, 10, 26, 18, 5, tzinfo=TZ).timestamp()),
        "description": "Silpo",
        "amount": -24500,
        "operationAmount": -24500,
        "currencyCode": 980,
    }
    row = to_row(item, ACCOUNT, FakeRates())

    assert row["id"] == "mono-ZuHWzqkKGVo="
    assert row["date"] == "2026-10-26 18:05"
    assert row["amount"] == -245.0
    assert row["currency"] == "UAH"
    assert row["amount_pln"] == -21.12
    assert row["account"] == "mono UAH *1234"
    assert row["merchant"] == "Silpo"
    assert row["source"] == "api"
    assert row["note"] == ""


def test_to_row_notes_foreign_operation_and_comment():
    item = {
        "id": "x",
        "time": int(datetime(2026, 10, 26, 9, 0, tzinfo=TZ).timestamp()),
        "description": "Biedronka",
        "comment": "groceries",
        "amount": -50000,
        "operationAmount": -4800,
        "currencyCode": 985,
    }
    row = to_row(item, ACCOUNT, FakeRates())

    assert row["description"] == "Biedronka: groceries"
    assert row["note"] == "operation: -48.00 PLN"
