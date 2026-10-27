"""Message handling: an update in, a reply out. No network, so everything here tests directly."""

import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Warsaw"))
AMOUNT = re.compile(r"\+?\d+(?:[.,]\d{1,2})?")
MAX_LIMIT = Decimal(100_000)
HELP = (
    "Send what you spent in cash, like: kebab 32 (or 32 kebab).\n"
    "BLIK payment: blik 50 allegro\n"
    "Income: +200 gift from mum\n"
    "/week shows this week's spending.\n"
    "/limit 600 changes the weekly limit.\n"
    "/categorize asks about shops without a category.\n"
    "Send a receipt photo to split a payment by item.\n"
    "/ask how much did I spend on groceries this month?"
)
ASK_EXAMPLES = (
    "Ask anything about your money, like:\n"
    "/ask how much did I spend on groceries this month?\n"
    "/ask what were my biggest payments last week?"
)
NO_CLAUDE_ASK = "Questions need CLAUDE_CODE_OAUTH_TOKEN in .env."


@dataclass(frozen=True)
class Reply:
    chat_id: int
    text: str
    keyboard: list | None = None
    edit_message_id: int | None = None  # edit this message instead of sending a new one
    callback_id: str | None = None  # button press to acknowledge


@dataclass(frozen=True)
class Cash:
    amount: Decimal  # negative for spending, positive for income
    merchant: str


def parse_cash(text: str) -> Cash | None:
    """One amount plus at least one word; a leading + means income."""
    tokens = text.split()
    found = [i for i, t in enumerate(tokens) if AMOUNT.fullmatch(t)]
    if len(found) != 1:
        return None
    raw = tokens.pop(found[0])
    value = Decimal(raw.lstrip("+").replace(",", "."))
    if value == 0 or not tokens:
        return None
    return Cash(value if raw.startswith("+") else -value, " ".join(tokens))


def week_start(now: datetime) -> datetime:
    local = now.astimezone(TZ)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _decimal(value) -> Decimal | None:
    if value in ("", None):
        return None
    return Decimal(str(value).replace(",", "."))


def spending(
    rows: list[dict], first_day: str, last_day: str, splits: dict | None = None
) -> tuple[Counter[str], Counter[str]]:
    """Spent per shop and per category for days first_day <= day < last_day (ISO dates).

    `splits` maps receipt_id -> category -> item total; such payments count by item share.
    """
    by_merchant: Counter[str] = Counter()
    by_category: Counter[str] = Counter()
    for row in rows:
        amount = _decimal(row.get("amount_pln"))
        if amount is None or amount >= 0 or row.get("category") == "Transfer":
            continue
        if first_day <= str(row.get("date", ""))[:10] < last_day:  # by day: bank rows may carry a date only
            by_merchant[row.get("merchant") or "?"] -= amount
            shares = (splits or {}).get(row.get("receipt_id") or "")
            items_total = sum(shares.values(), Decimal(0)) if shares else Decimal(0)
            if items_total > 0:
                for category, value in shares.items():  # scaled: the bank amount wins over item rounding
                    by_category[category] -= amount * value / items_total
            else:
                by_category[row.get("category") or "Uncategorised"] -= amount
    return by_merchant, by_category


def budget_status(spent: Decimal, limit: Decimal) -> str:
    left = limit - spent
    status = f"{left:.2f} left" if left >= 0 else f"over by {-left:.2f}"
    return f"spent {spent:.2f} of {limit:.2f} PLN, {status}"


def week_summary(rows: list[dict], limit: Decimal, now: datetime, splits: dict | None = None) -> str:
    start = week_start(now)
    end = (start + timedelta(days=7)).date().isoformat()
    by_merchant, by_category = spending(rows, start.date().isoformat(), end, splits)
    spent = sum(by_merchant.values(), Decimal(0))
    text = f"This week (from {start:%a %d %b}): {budget_status(spent, limit)}."
    if by_merchant:
        text += "\nTop: " + ", ".join(f"{m} {v:.2f}" for m, v in by_merchant.most_common(3))
        text += "\nBy category: " + ", ".join(f"{c} {v:.2f}" for c, v in by_category.most_common(3))
    return text


def set_limit(args: list[str], now: datetime, store) -> str:
    if not args:
        return f"Weekly limit is {store.weekly_limit():.2f} PLN. Change it with /limit 600"
    valid = len(args) == 1 and AMOUNT.fullmatch(args[0]) and not args[0].startswith("+")
    value = _decimal(args[0]) if valid else None
    if value is None or not 0 < value <= MAX_LIMIT:
        return f"Limit must be one number above 0 and up to {MAX_LIMIT:.0f}, like /limit 600"
    store.set_weekly_limit(value)
    summary = week_summary(store.rows(), value, now, store.receipt_splits())
    return f"Weekly limit set to {value:.2f} PLN.\n{summary}"


def handle(text: str, message_id: int, now: datetime, store) -> str:
    command = text.split()[0].split("@")[0].lower() if text.split() else ""
    if command == "/week":
        return week_summary(store.rows(), store.weekly_limit(), now, store.receipt_splits())
    if command == "/limit":
        return set_limit(text.split()[1:], now, store)
    cash = parse_cash(text)
    if cash is None:
        return HELP
    words = cash.merchant.split()
    blik = any(w.lower() == "blik" for w in words)  # BLIK is paid from a bank account, not cash
    merchant = " ".join(w for w in words if w.lower() != "blik") or "BLIK"
    account = (os.environ.get("BLIK_ACCOUNT") or "blik") if blik else "cash"
    row = {
        "id": f"tg-{message_id}",  # Telegram re-delivers after a crash; the ID stops a double entry
        "date": now.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        "amount": float(cash.amount),
        "currency": "PLN",
        "amount_pln": float(cash.amount),
        "account": account,
        "merchant": merchant,
        "description": text,
        "source": "manual",
    }
    if not store.add(row):
        return "Already logged."
    logged = f"Logged {merchant} {cash.amount:+.2f} PLN ({account})."
    return f"{logged}\n{week_summary(store.rows(), store.weekly_limit(), now, store.receipt_splits())}"


def _image_file_id(msg: dict) -> str | None:
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"]  # Telegram lists sizes smallest first
    doc = msg.get("document") or {}
    return doc.get("file_id") if str(doc.get("mime_type", "")).startswith("image/") else None


def process(
    update: dict, allowed_id: int, store, now: datetime, asker, desk, oracle=None
) -> list[Reply] | None:
    """Replies for one Telegram update, or None when it doesn't come from the owner."""
    if press := update.get("callback_query"):
        if press.get("from", {}).get("id") != allowed_id:
            return None
        msg = press["message"]
        data = press.get("data", "")
        text = desk.answer(data, store) if data.startswith("r:") else asker.answer(data, store)
        return [Reply(msg["chat"]["id"], text, edit_message_id=msg["message_id"], callback_id=press["id"])]
    msg = update.get("message")
    if not msg or msg.get("from", {}).get("id") != allowed_id:
        return None
    chat = msg["chat"]["id"]
    if file_id := _image_file_id(msg):
        text, keyboard = desk.from_photo(file_id, store, now)
        return [Reply(chat, text, keyboard)]
    text = msg.get("text")
    if text is None:
        return [Reply(chat, "I can read text and receipt photos.")]
    command, _, rest = text.strip().partition(" ")
    command = command.split("@")[0].lower()
    if command == "/categorize":
        questions = asker.ask(store.rows(), store.categories())
        return [Reply(chat, q, k) for q, k in questions] or [Reply(chat, "Nothing new to categorise.")]
    if command == "/ask":
        if oracle is None:
            return [Reply(chat, NO_CLAUDE_ASK)]
        if not rest.strip():
            return [Reply(chat, ASK_EXAMPLES)]
        return [Reply(chat, oracle.answer(chat, rest.strip(), store, now))]
    return [Reply(chat, handle(text, msg["message_id"], now, store))]
