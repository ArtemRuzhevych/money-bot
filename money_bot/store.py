"""The Google Sheet: the bot's only storage."""

import time
from decimal import Decimal

import gspread

from money_bot import categories, receipts, wallet
from money_bot.receipts import Receipt

COLUMNS = [
    "id",
    "date",
    "amount",
    "currency",
    "amount_pln",
    "account",
    "merchant",
    "description",
    "category",
    "source",
    "receipt_id",
    "note",
]
DEFAULT_LIMIT = Decimal(500)


class Store:
    def __init__(self, sheet):
        self.tx = self._tab(sheet, "transactions", COLUMNS)
        self.budget = self._tab(sheet, "budget", ["weekly_limit", 500])
        self.state_tab = self._tab(sheet, "state", ["key", "value"])
        self.cats_tab = self._tab(
            sheet, "categories", ["category"], [[c] for c in categories.DEFAULT_CATEGORIES]
        )
        self.rules_tab = self._tab(sheet, "rules", ["pattern", "category"], categories.starter_rules())
        self.receipts_tab = self._tab(sheet, "receipts", receipts.RECEIPT_COLUMNS)
        self.items_tab = self._tab(sheet, "receipt_items", receipts.ITEM_COLUMNS)
        self.inbox = self._tab(sheet, "inbox", wallet.INBOX_COLUMNS)
        # receipt_id -> (sheet row, record)
        self.receipt_rows = {
            r["receipt_id"]: (i, r) for i, r in enumerate(self.receipts_tab.get_all_records(), 2)
        }
        self._rules: list[tuple[str, str]] | None = None
        self._rules_at = 0.0
        self.ids = set(self.tx.col_values(1)[1:])
        # key -> (value, sheet row); rows start at 2, under the header
        self.kv = {r["key"]: (r["value"], i) for i, r in enumerate(self.state_tab.get_all_records(), 2)}

    @classmethod
    def open(cls, service_account_file: str, sheet_id: str) -> "Store":
        return cls(gspread.service_account(filename=service_account_file).open_by_key(sheet_id))

    @staticmethod
    def _tab(sheet, title: str, header: list, seed=()):
        try:
            ws = sheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(title, rows=1000, cols=len(header))
        first = ws.row_values(1)
        if not first:  # seeded only when empty, never over the owner's edits
            ws.update([header, *[list(r) for r in seed]], "A1")
        elif title == "transactions" and first != header:
            # Never write rows into columns that mean something else.
            raise ValueError(f"'{title}' tab has columns {first}, expected {header}")
        return ws

    def add(self, row: dict) -> bool:
        """Append a transaction; False if its ID is already in the Sheet."""
        if row["id"] in self.ids:
            return False
        if not row.get("category"):
            row = {**row, "category": categories.pick(row, self.rules()) or ""}
        self.tx.append_row([row.get(c, "") for c in COLUMNS], value_input_option="RAW")
        self.ids.add(row["id"])
        self._link_pending(row)
        return True

    def _link_pending(self, row: dict) -> None:
        """A receipt marked 'card' links to the first payment that matches it."""
        for rid, (_, rec) in self.receipt_rows.items():
            if rec["status"] != "pending":
                continue
            receipt = Receipt(rid, str(rec["date"]), str(rec["shop"]), Decimal(str(rec["total"])), "PLN", [])
            if receipts.matches(row, receipt) is None:
                continue
            # Look the row up now rather than count rows: rows may have been deleted in the Sheet.
            number = self.tx.col_values(1).index(row["id"]) + 1
            self.link(number, rid, receipts.main_of(self.receipt_splits().get(rid, {})))
            self._set_receipt_status(rid, "linked", row["id"])
            return

    def rows(self) -> list[dict]:
        # shortcut: reads the whole tab per call; fine for a few thousand rows, cache it if /week gets slow.
        return self.tx.get_all_records()

    def weekly_limit(self) -> Decimal:
        value = self.budget.acell("B1").value
        return Decimal(str(value).replace(",", ".")) if value not in (None, "") else DEFAULT_LIMIT

    def set_weekly_limit(self, value: Decimal) -> None:
        self.budget.update([["weekly_limit", float(value)]], "A1")

    def categories(self) -> list[str]:
        return [c for c in self.cats_tab.col_values(1)[1:] if c]

    def rules(self) -> list[tuple[str, str]]:
        # Cached 5 min so a sync burst doesn't read the tab per row; Sheet edits show up within that.
        if self._rules is None or time.monotonic() - self._rules_at > 300:
            self._rules = [(str(r["pattern"]), r["category"]) for r in self.rules_tab.get_all_records()]
            self._rules_at = time.monotonic()
        return self._rules

    def add_rule(self, pattern: str, category: str) -> None:
        self.rules_tab.append_row([pattern, category], value_input_option="RAW")
        self.rules().append((pattern, category))

    def recategorize(self) -> int:
        """Fill the category of every uncategorised row a rule now matches; returns how many."""
        col = chr(ord("A") + COLUMNS.index("category"))
        rules = self.rules()
        updates = []
        for i, row in enumerate(self.rows(), 2):
            found = None if row.get("category") else categories.pick(row, rules)
            if found:
                updates.append({"range": f"{col}{i}", "values": [[found]]})
        if updates:
            self.tx.batch_update(updates)
        return len(updates)

    def has_receipt(self, receipt_id: str) -> bool:
        return receipt_id in self.receipt_rows

    def save_receipt(self, receipt: Receipt, status: str, transaction_id: str) -> None:
        record = [receipt.id, receipt.date, receipt.shop, float(receipt.total), status, transaction_id]
        self.receipts_tab.append_row(record, value_input_option="RAW")
        items = [[receipt.id, i["name"], i["qty"], float(i["price"]), i["category"]] for i in receipt.items]
        if items:
            self.items_tab.append_rows(items, value_input_option="RAW")
        self.receipt_rows[receipt.id] = (
            len(self.receipt_rows) + 2,
            dict(zip(receipts.RECEIPT_COLUMNS, record, strict=False)),
        )

    def _set_receipt_status(self, receipt_id: str, status: str, transaction_id: str) -> None:
        number, record = self.receipt_rows[receipt_id]
        self.receipts_tab.batch_update(
            [
                {"range": f"E{number}", "values": [[status]]},
                {"range": f"F{number}", "values": [[transaction_id]]},
            ]
        )
        record.update(status=status, transaction_id=transaction_id)

    def link(self, row_number: int, receipt_id: str, category: str) -> None:
        col = {c: chr(ord("A") + COLUMNS.index(c)) for c in ("receipt_id", "category")}
        self.tx.batch_update(
            [
                {"range": f"{col['receipt_id']}{row_number}", "values": [[receipt_id]]},
                {"range": f"{col['category']}{row_number}", "values": [[category]]},
            ]
        )

    def receipt_items(self) -> list[dict]:
        return self.items_tab.get_all_records()

    def inbox_rows(self) -> list[dict]:
        return self.inbox.get_all_records()

    def clear_inbox(self, count: int) -> None:
        """Deletes the first `count` payments; the Apps Script only appends, so newer ones stay."""
        self.inbox.delete_rows(2, count + 1)

    def receipt_splits(self) -> dict[str, dict[str, Decimal]]:
        """receipt_id -> category -> sum of item prices."""
        splits: dict[str, dict[str, Decimal]] = {}
        for item in self.receipt_items():
            shares = splits.setdefault(item["receipt_id"], {})
            category = item["category"] or "Other"
            shares[category] = shares.get(category, Decimal(0)) + Decimal(
                str(item["price"]).replace(",", ".")
            )
        return splits

    def state(self, key: str):
        return self.kv[key][0] if key in self.kv else None

    def set_state(self, key: str, value) -> None:
        if key in self.kv:
            row = self.kv[key][1]
            self.state_tab.update([[key, value]], f"A{row}")
        else:
            row = len(self.kv) + 2
            self.state_tab.append_row([key, value], value_input_option="RAW")
        self.kv[key] = (value, row)
