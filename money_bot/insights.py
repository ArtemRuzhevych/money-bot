"""The bot speaking up: the 21:00 digest, the Sunday summary and /ask questions answered by Claude."""

import csv
import json
import logging
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from money_bot import claude
from money_bot.handlers import TZ, _decimal, budget_status, spending, week_start

SUMMARY_HOUR = 20
SNAPSHOT_DAYS = 365
TRANSACTION_COLUMNS = ["date", "amount_pln", "account", "merchant", "category", "receipt_id"]
ITEM_COLUMNS = ["receipt_id", "item", "qty", "price", "category"]
LOOKING = "Looking…"
FAILED = "Couldn't answer that, it's in the log."
REMARK_PROMPT = (
    "This is my spending summary for the week, in PLN:\n\n{numbers}\n\n"
    "Write one or two plain sentences on what stood out. No Markdown, no greeting, "
    "no investment advice. Reply with only the sentences."
)
ASK_PROMPT = (
    "You answer the user's questions about their own money. Today is {today} ({tz}). "
    "transactions.csv in this folder has their transactions for the last 12 months: amount_pln is in PLN, "
    "negative is spending, positive is income, and category Transfer is money moved between their own "
    "accounts (not spending). receipt_items.csv has the items of receipts linked by receipt_id. "
    "Weeks run Monday to Sunday. Their weekly spending limit is {limit} PLN. Categories: {cats}. "
    "Not every account may be tracked, so say when the data can't answer. "
    "Answer in 2 to 6 plain lines, no Markdown. Never give investment advice or suggest moving money.\n\n"
    "Question: {question}"
)
log = logging.getLogger(__name__)


def _day(row: dict) -> str:
    return str(row.get("date", ""))[:10]


def _total(counter: Counter) -> Decimal:
    return sum(counter.values(), Decimal(0))


def digest(rows: list[dict], limit: Decimal, now: datetime, splits: dict | None = None) -> str | None:
    """Today's payments and the week so far, or None when nothing happened today."""
    today = now.astimezone(TZ).date()
    by_merchant, _ = spending(rows, today.isoformat(), (today + timedelta(days=1)).isoformat(), splits)
    amounts = [
        _decimal(r.get("amount_pln")) or Decimal(0)
        for r in rows
        if _day(r) == today.isoformat() and r.get("category") != "Transfer"
    ]
    count = sum(1 for a in amounts if a < 0)
    income = sum((a for a in amounts if a > 0), Decimal(0))
    if not count and not income:
        return None
    lines = []
    if count:
        top = ", ".join(f"{m} {v:.2f}" for m, v in by_merchant.most_common(3))
        payments = "payment" if count == 1 else "payments"
        lines.append(f"Today: {count} {payments}, {_total(by_merchant):.2f} PLN ({top}).")
    if income:
        lines.append(f"Today in: +{income:.2f} PLN.")
    start = week_start(now).date()
    week, _ = spending(rows, start.isoformat(), (start + timedelta(days=7)).isoformat(), splits)
    lines.append(f"This week: {budget_status(_total(week), limit)}.")
    return "\n".join(lines)


def summary_due(now: datetime, store) -> bool:
    local = now.astimezone(TZ)
    return (
        local.weekday() == 6
        and local.hour >= SUMMARY_HOUR
        and store.state("summary:last") != week_start(now).date().isoformat()
    )


def _change(now_value: Decimal, before: Decimal) -> str:
    if not before:
        return "new"
    diff = now_value - before
    return f"{diff:+.2f}"


def summary(rows: list[dict], limit: Decimal, now: datetime, splits: dict | None = None, ask=None) -> str:
    """The Sunday numbers, plus a line or two from Claude when `ask` is given and works."""
    start = week_start(now).date()
    shops, cats = spending(rows, start.isoformat(), (start + timedelta(days=7)).isoformat(), splits)
    _, last_cats = spending(rows, (start - timedelta(days=7)).isoformat(), start.isoformat(), splits)
    spent, last = _total(shops), _total(last_cats)
    if not spent:
        lines = [f"Week from {start:%a %d %b}: nothing spent this week."]
    else:
        lines = [f"Week from {start:%a %d %b}: {budget_status(spent, limit)}."]
    if last and spent:
        lines.append(f"Last week {last:.2f}, so {(spent - last) / last:+.0%} this week.")
    elif last:
        lines.append(f"Last week {last:.2f}.")
    if spent:
        lines.append(
            "Categories: "
            + ", ".join(f"{c} {v:.2f} ({_change(v, last_cats[c])})" for c, v in cats.most_common(3))
            + "."
        )
        lines.append("Shops: " + ", ".join(f"{m} {v:.2f}" for m, v in shops.most_common(3)) + ".")
    loose = sum(
        1
        for r in rows
        if (_decimal(r.get("amount_pln")) or 0) < 0 and not r.get("category") and not r.get("receipt_id")
    )
    if loose:
        lines.append(f"{loose} payment{'s have' if loose > 1 else ' has'} no category, send /categorize.")
    text = "\n".join(lines)
    if ask and spent:
        try:
            with tempfile.TemporaryDirectory(prefix="summary-") as folder:  # empty: Read has nothing to see
                out = ask(REMARK_PROMPT.format(numbers=text), cwd=Path(folder), model="sonnet", timeout=120)
            remark = out["result"].strip()
            if remark:
                text += f"\n\n{remark}"
        except Exception:
            log.exception("summary remark failed, sending the numbers alone")
    return text


def snapshot(folder: Path, rows: list[dict], items: list[dict], today: date) -> None:
    """The last 12 months as CSV for Claude, without descriptions and notes."""
    first = (today - timedelta(days=SNAPSHOT_DAYS)).isoformat()
    kept = [r for r in rows if _day(r) >= first]
    receipts = {r.get("receipt_id") for r in kept if r.get("receipt_id")}
    for name, columns, data in [
        ("transactions.csv", TRANSACTION_COLUMNS, kept),
        ("receipt_items.csv", ITEM_COLUMNS, [i for i in items if i.get("receipt_id") in receipts]),
    ]:
        with open(Path(folder) / name, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)


class Oracle:
    """/ask: Claude Sonnet reads a fresh snapshot of the Sheet in a temp folder and answers."""

    def __init__(self, send, ask=claude.ask):
        self.send, self.ask = send, ask

    def answer(self, chat_id: int, question: str, store, now: datetime) -> str:
        self.send(chat_id, LOOKING)  # Sonnet takes 10 to 30 s
        try:
            with tempfile.TemporaryDirectory(prefix="ask-") as folder:
                snapshot(Path(folder), store.rows(), store.receipt_items(), now.astimezone(TZ).date())
                prompt = ASK_PROMPT.format(
                    today=now.astimezone(TZ).date().isoformat(),
                    tz=TZ.key,
                    limit=f"{store.weekly_limit():.2f}",
                    cats=json.dumps(store.categories(), ensure_ascii=False),
                    question=question,
                )
                out = self.ask(prompt, cwd=Path(folder), model="sonnet", timeout=180)
        except Exception:
            log.exception("answering a question failed")
            return FAILED
        return out["result"].strip() or FAILED
