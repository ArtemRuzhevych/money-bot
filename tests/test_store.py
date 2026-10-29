from decimal import Decimal

import gspread
import pytest
from gspread.utils import numericise

from money_bot import wallet
from money_bot.receipts import Receipt
from money_bot.store import COLUMNS, Store


class FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self.data = [list(r) for r in rows or []]

    def row_values(self, n):
        return self.data[n - 1] if len(self.data) >= n else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.data]

    def append_row(self, values, value_input_option=None):
        self.data.append(list(values))

    def append_rows(self, rows, value_input_option=None):
        self.data.extend(list(r) for r in rows)

    def update(self, values, range_name):
        start = int(range_name[1:]) - 1  # "A<n>"
        for i, r in enumerate(values, start):
            if i < len(self.data):
                self.data[i] = list(r)
            else:
                self.data.append(list(r))

    def batch_update(self, updates):
        for u in updates:
            col, row = ord(u["range"][0]) - ord("A"), int(u["range"][1:]) - 1
            self.data[row] += [""] * (col + 1 - len(self.data[row]))
            self.data[row][col] = u["values"][0][0]

    def delete_rows(self, start, end):
        del self.data[start - 1 : end]

    def get_all_records(self, value_render_option=None):
        header, *body = self.data
        if value_render_option != "UNFORMATTED_VALUE":  # like a Sheet in a comma-decimal locale
            body = [
                [numericise(str(v).replace(".", ",")) if isinstance(v, float) else v for v in r] for r in body
            ]
        return [dict(zip(header, r, strict=False)) for r in body]

    def acell(self, label):
        assert label == "B1"
        value = self.data[0][1] if self.data and len(self.data[0]) > 1 else None
        return type("Cell", (), {"value": value})()


class FakeSpreadsheet:
    def __init__(self, tabs=()):
        self.tabs = {t.title: t for t in tabs}

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]


def tx(id_, amount=-1):
    return {"id": id_, "date": "2026-10-26", "amount": amount, "amount_pln": amount, "merchant": "m"}


def test_creates_missing_tabs_with_headers_and_default_limit():
    sheet = FakeSpreadsheet()
    store = Store(sheet)

    assert sheet.tabs["transactions"].data == [COLUMNS]
    assert sheet.tabs["budget"].data == [["weekly_limit", 500]]
    assert store.weekly_limit() == Decimal(500)


def test_adds_row_in_column_order_and_skips_duplicates():
    sheet = FakeSpreadsheet()
    store = Store(sheet)

    assert store.add(tx("tg-1")) is True
    assert store.add(tx("tg-1")) is False
    data = sheet.tabs["transactions"].data
    assert len(data) == 2
    assert data[1][COLUMNS.index("id")] == "tg-1"
    assert data[1][COLUMNS.index("note")] == ""


def test_remembers_existing_ids_after_restart():
    sheet = FakeSpreadsheet([FakeWorksheet("transactions", [COLUMNS, ["tg-9"] + [""] * (len(COLUMNS) - 1)])])
    assert Store(sheet).add(tx("tg-9")) is False


def test_reads_rows_and_edited_limit():
    sheet = FakeSpreadsheet([FakeWorksheet("budget", [["weekly_limit", "450,50"]])])
    store = Store(sheet)
    store.add(tx("tg-1", -5))

    assert store.rows()[0]["id"] == "tg-1"
    assert store.weekly_limit() == Decimal("450.50")


def test_empty_limit_cell_falls_back_to_default():
    sheet = FakeSpreadsheet([FakeWorksheet("budget", [["weekly_limit"]])])
    assert Store(sheet).weekly_limit() == Decimal(500)


def test_refuses_a_transactions_tab_with_other_columns():
    sheet = FakeSpreadsheet([FakeWorksheet("transactions", [["date", "amount"]])])
    with pytest.raises(ValueError, match="transactions"):
        Store(sheet)


def test_set_weekly_limit_writes_b1():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.set_weekly_limit(Decimal("650.50"))

    assert sheet.tabs["budget"].data[0] == ["weekly_limit", 650.5]
    assert store.weekly_limit() == Decimal("650.5")


def test_state_is_created_saved_and_read_back_after_restart():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    assert store.state("mono:a") is None

    store.set_state("mono:a", 100)
    store.set_state("mono:b", 5)
    store.set_state("mono:a", 200)

    assert sheet.tabs["state"].data == [["key", "value"], ["mono:a", 200], ["mono:b", 5]]
    assert Store(sheet).state("mono:a") == 200


def test_categories_and_rules_are_seeded_once():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    assert store.categories()[:2] == ["Groceries", "Eating out"]
    assert ("biedronka", "Groceries") in store.rules()

    sheet.tabs["categories"].data = [["category"], ["Food"]]  # the owner edits the tab
    sheet.tabs["rules"].data = [["pattern", "category"]]
    store = Store(sheet)
    assert store.categories() == ["Food"]
    assert store.rules() == []


def test_add_categorizes_from_rules():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.add({"id": "tg-1", "date": "2026-10-26", "amount_pln": -40, "merchant": "Biedronka 12"})
    store.add({"id": "tg-2", "date": "2026-10-26", "amount_pln": -9, "merchant": "Bar Mleczny Pod Lipami"})

    rows = store.rows()
    assert [r["category"] for r in rows] == ["Groceries", ""]


def test_new_rule_fills_only_uncategorized_matching_rows():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.add({"id": "1", "amount_pln": -9, "merchant": "Bar Mleczny Pod Lipami"})
    store.add({"id": "2", "amount_pln": -12, "merchant": "BAR MLECZNY POD LIPAMI"})
    store.add({"id": "3", "amount_pln": -40, "merchant": "Biedronka"})
    store.add({"id": "4", "amount_pln": -5, "merchant": "Bar Mleczny Pod Lipami", "category": "Gifts"})

    store.add_rule("bar mleczny pod lipami", "Eating out")

    assert store.recategorize() == 2
    assert [r["category"] for r in store.rows()] == ["Eating out", "Eating out", "Groceries", "Gifts"]
    assert ["bar mleczny pod lipami", "Eating out"] in sheet.tabs["rules"].data
    assert store.recategorize() == 0


def receipt(rid="rcpt-1", total="47.13", day="2026-10-24"):
    items = [
        {"name": "food", "qty": 1, "price": Decimal("36.15"), "category": "Groceries"},
        {"name": "drinks", "qty": 2, "price": Decimal("10.98"), "category": "Fun"},
    ]
    return Receipt(rid, day, "Biedronka", Decimal(total), "PLN", items)


def test_saved_receipt_and_items_survive_a_restart():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.save_receipt(receipt(), "cash", "rcpt-1")

    assert sheet.tabs["receipts"].data[1] == ["rcpt-1", "2026-10-24", "Biedronka", 47.13, "cash", "rcpt-1"]
    assert sheet.tabs["receipt_items"].data[1:] == [
        ["rcpt-1", "food", 1, 36.15, "Groceries"],
        ["rcpt-1", "drinks", 2, 10.98, "Fun"],
    ]
    assert Store(sheet).has_receipt("rcpt-1")
    assert not Store(sheet).has_receipt("rcpt-2")


def test_link_sets_receipt_id_and_category():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.add({"id": "mono-1", "date": "2026-10-24", "amount_pln": -47.13, "merchant": "Bar"})
    store.link(2, "rcpt-1", "Groceries")

    row = store.rows()[0]
    assert (row["receipt_id"], row["category"]) == ("rcpt-1", "Groceries")


def test_pending_receipt_links_when_its_bank_payment_arrives():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.add({"id": "mono-0", "date": "2026-10-24", "amount_pln": -5, "merchant": "Bar"})
    store.save_receipt(receipt(), "pending", "")

    store.add({"id": "mono-1", "date": "2026-10-25 09:00", "amount_pln": -47.13, "merchant": "BIEDRONKA 12"})

    row = store.rows()[1]
    assert (row["receipt_id"], row["category"]) == ("rcpt-1", "Groceries")
    assert sheet.tabs["receipts"].data[1][4:] == ["linked", "mono-1"]
    store.add({"id": "mono-2", "date": "2026-10-25", "amount_pln": -47.13, "merchant": "BIEDRONKA 12"})
    assert store.rows()[2]["receipt_id"] == ""  # a receipt links only once


def test_receipt_splits_for_week():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.save_receipt(receipt(), "linked", "mono-1")
    assert store.receipt_splits() == {"rcpt-1": {"Groceries": Decimal("36.15"), "Fun": Decimal("10.98")}}
    assert [i["receipt_id"] for i in store.receipt_items()] == ["rcpt-1"] * len(receipt().items)


def test_inbox_tab_reads_and_clears_from_the_top():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    inbox = sheet.tabs["inbox"]
    assert inbox.data == [wallet.INBOX_COLUMNS]

    inbox.append_rows([["t1", "Millennium", "Biedronka", "47,13 zł", ""], ["t2", "PUMB", "Lidl", "9 zł", ""]])
    assert [r["merchant"] for r in store.inbox_rows()] == ["Biedronka", "Lidl"]

    inbox.append_row(["t3", "Millennium", "Żabka", "5 zł", ""])  # arrives while the bot works
    store.clear_inbox(2)
    assert [r["merchant"] for r in store.inbox_rows()] == ["Żabka"]


def test_amounts_read_back_exactly_in_a_comma_decimal_locale():
    sheet = FakeSpreadsheet()
    store = Store(sheet)
    store.add({**tx("w1"), "amount": -39.55, "amount_pln": -39.55})

    assert store.rows()[0]["amount_pln"] == -39.55  # not -3955
