"""Receipt photos: read by Claude, linked to the bank payment, split across categories by item."""

import hashlib
import json
import logging
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from money_bot import claude
from money_bot.categories import fold, parse_json
from money_bot.handlers import TZ

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.02")  # rounding on receipts with weighed items
EXPIRED = "This receipt expired, send the photo again"
NO_CLAUDE = "Reading receipts needs CLAUDE_CODE_OAUTH_TOKEN in .env."
RECEIPT_COLUMNS = ["receipt_id", "date", "shop", "total", "status", "transaction_id"]
ITEM_COLUMNS = ["receipt_id", "item", "qty", "price", "category"]
PROMPT = (
    "Read the receipt image {name} in this folder. It is most likely from Poland. "
    "Reply with only JSON, no prose: "
    '{{"shop": str, "date": "YYYY-MM-DD", "currency": "ISO code", "total": number, '
    '"items": [{{"name": str, "qty": number, "price": number, "category": str}}]}}. '
    "price is the line total after discounts; a discount on its own line is an item with a negative price. "
    "category must be one of {cats}. "
    'If the image is not a receipt, reply {{"error": "not a receipt"}}.'
)
log = logging.getLogger(__name__)


class NotAReceipt(ValueError):
    pass


@dataclass
class Receipt:
    id: str
    date: str  # YYYY-MM-DD
    shop: str
    total: Decimal
    currency: str
    items: list[dict]  # name, qty, price (Decimal), category


def _decimal(value) -> Decimal:
    return Decimal(str(value).replace(",", ".")).quantize(CENT)


def read(image: bytes, suffix: str, cats: list[str], today: date, ask=claude.ask) -> Receipt:
    """Claude reads the photo from a temp folder holding only that image (its Read tool sees nothing else)."""
    name = f"receipt{suffix}"
    with tempfile.TemporaryDirectory(prefix="receipt-") as folder:
        (Path(folder) / name).write_bytes(image)
        out = ask(
            PROMPT.format(name=name, cats=json.dumps(cats)), cwd=Path(folder), model="haiku", timeout=120
        )
    data = parse_json(out["result"])
    if "error" in data or "total" not in data:
        raise NotAReceipt(str(data.get("error", "no total")))
    try:
        day = date.fromisoformat(str(data.get("date"))).isoformat()
    except ValueError:
        day = today.isoformat()
    shop = str(data.get("shop") or "?").strip()
    total = _decimal(data["total"])
    items = [
        {
            "name": str(i.get("name", "?")),
            "qty": i.get("qty", 1),
            "price": _decimal(i.get("price", 0)),
            "category": i.get("category") if i.get("category") in cats else "Other",
        }
        for i in data.get("items", [])
    ]
    digest = hashlib.sha1(f"{fold(shop)}|{day}|{total}".encode()).hexdigest()[:12]
    return Receipt(f"rcpt-{digest}", day, shop, total, str(data.get("currency") or "PLN").upper(), items)


def split(receipt: Receipt) -> dict[str, Decimal]:
    shares: Counter[str] = Counter()
    for item in receipt.items:
        shares[item["category"]] += item["price"]
    return dict(shares)


def main_of(shares: dict[str, Decimal]) -> str:
    return max(shares, key=lambda c: shares[c]) if shares else "Other"


def main_category(receipt: Receipt) -> str:
    return main_of(split(receipt))


def problems(receipt: Receipt) -> list[str]:
    items_total = sum((i["price"] for i in receipt.items), Decimal(0))
    if abs(items_total - receipt.total) > TOLERANCE:
        return [f"items add up to {items_total:.2f}, not {receipt.total:.2f}"]
    return []


def matches(row: dict, receipt: Receipt) -> int | None:
    """Days between receipt and payment (0 or 1) if this transaction can be that receipt's payment."""
    if row.get("receipt_id") or row.get("amount_pln") in ("", None):
        return None
    if abs(_decimal(row["amount_pln"]) + receipt.total) > CENT:
        return None
    try:
        gap = (date.fromisoformat(str(row.get("date", ""))[:10]) - date.fromisoformat(receipt.date)).days
    except ValueError:
        return None
    return gap if gap in (0, 1) else None  # card payments can settle the next day


def find_match(rows: list[dict], receipt: Receipt) -> tuple[int, dict] | None:
    """(sheet row number, transaction) of the closest-dated unlinked payment for the receipt."""
    best = None
    for number, row in enumerate(rows, 2):
        gap = matches(row, receipt)
        if gap is not None and (best is None or gap < best[0]):
            best = (gap, number, row)
    return (best[1], best[2]) if best else None


def describe(receipt: Receipt) -> str:
    day = date.fromisoformat(receipt.date)
    lines = [f"{receipt.shop}, {day:%d %b}, {receipt.total:.2f} PLN, {len(receipt.items)} items."]
    shares = sorted(split(receipt).items(), key=lambda kv: -kv[1])
    if shares:
        lines.append(", ".join(f"{c} {v:.2f}" for c, v in shares))
    lines += [f"Check the Sheet: {p}" for p in problems(receipt)]
    if receipt.currency != "PLN":
        lines.append(f"The receipt is in {receipt.currency}; it's counted as PLN for now.")
    return "\n".join(lines)


class ReceiptDesk:
    """Turns photos into receipts and remembers the unmatched ones until the owner picks cash or card."""

    def __init__(self, download, ask=claude.ask):
        self.download, self.ask = download, ask
        self.waiting: dict[str, Receipt] = {}

    def from_photo(self, file_id: str, store, now: datetime) -> tuple[str, list | None]:
        if self.ask is None:
            return NO_CLAUDE, None
        try:
            image, suffix = self.download(file_id)
            receipt = read(image, suffix, store.categories(), now.astimezone(TZ).date(), ask=self.ask)
        except NotAReceipt:
            return "That doesn't look like a receipt.", None
        except Exception:
            log.exception("reading a receipt failed")
            return "Couldn't read that receipt. Try a sharper photo, or send it as a file.", None
        if store.has_receipt(receipt.id):
            return "Already have this receipt.", None
        text = describe(receipt)
        found = find_match(store.rows(), receipt)
        if found:
            number, row = found
            store.save_receipt(receipt, "linked", row["id"])
            store.link(number, receipt.id, main_category(receipt))
            return f"{text}\nLinked to {row.get('account') or row['id']} ✓", None
        self.waiting[receipt.id] = receipt
        buttons = [
            {"text": "Cash", "callback_data": f"r:{receipt.id}:cash"},
            {"text": "Card, link later", "callback_data": f"r:{receipt.id}:card"},
        ]
        return f"{text}\nNo bank payment matches yet.", [buttons]

    def answer(self, data: str, store) -> str:
        parts = data.split(":")
        if (
            len(parts) != 3
            or parts[0] != "r"
            or parts[2] not in ("cash", "card")
            or parts[1] not in self.waiting
        ):
            return EXPIRED
        receipt = self.waiting.pop(parts[1])
        if parts[2] == "card":
            store.save_receipt(receipt, "pending", "")
            return f"{receipt.shop}: waiting for the card payment to link"
        store.save_receipt(receipt, "cash", receipt.id)
        store.add(
            {
                "id": receipt.id,
                "date": receipt.date,
                "amount": float(-receipt.total),
                "currency": "PLN",
                "amount_pln": float(-receipt.total),
                "account": "cash",
                "merchant": receipt.shop,
                "description": "from a receipt photo",
                "category": main_category(receipt),
                "source": "receipt",
                "receipt_id": receipt.id,
            }
        )
        return f"{receipt.shop} {receipt.total:.2f} PLN saved as cash ✓"
