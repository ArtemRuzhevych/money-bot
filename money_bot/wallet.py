"""Apple Pay payments: an iPhone Shortcuts automation posts them to an Apps Script inbox in the Sheet."""

import hashlib
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from money_bot.handlers import TZ

INBOX_COLUMNS = ["received", "card", "merchant", "amount", "time"]  # apps_script/inbox.gs writes these
SYMBOLS = {"zł": "PLN", "zl": "PLN", "€": "EUR", "$": "USD", "£": "GBP", "₴": "UAH", "грн": "UAH"}
NUMBER = re.compile(r"\d[\d\s.,  ]*")
log = logging.getLogger(__name__)


def parse_amount(value) -> tuple[Decimal, str]:
    """`47,13 zł`, `PLN 47.13`, `€2.00`, `1 234,56 zł` -> (amount, currency); PLN when none is named."""
    text = str(value)
    match = NUMBER.search(text)
    if not match:
        raise ValueError(f"no amount in {text!r}")
    digits = re.sub(r"[\s  ]", "", match.group()).rstrip(".,")
    last = max(digits.rfind("."), digits.rfind(","))
    if last >= 0 and len(digits) - last - 1 in (1, 2):  # 1-2 digits after the last mark: decimals
        digits = re.sub(r"[.,]", "", digits[:last]) + "." + digits[last + 1 :]
    else:  # otherwise the marks group thousands
        digits = re.sub(r"[.,]", "", digits)
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        raise ValueError(f"no amount in {text!r}") from None
    if not amount:
        raise ValueError(f"zero amount in {text!r}")
    rest = (text[: match.start()] + text[match.end() :]).strip()
    code = re.search(r"\b[A-Z]{3}\b", rest)
    currency = code.group() if code else next((c for s, c in SYMBOLS.items() if s in rest.lower()), "PLN")
    return amount, currency


def _account(card: str) -> str:
    """CARD_ACCOUNTS="millennium=millennium,pumb=pumb": a card named like the text gets that account."""
    folded = card.lower()
    for pair in os.environ.get("CARD_ACCOUNTS", "").split(","):
        text, _, account = pair.partition("=")
        if text.strip() and account.strip() and text.strip().lower() in folded:
            return account.strip()
    return card.strip() or "wallet"


def _when(item: dict) -> datetime:
    for key in ("time", "received"):
        try:
            return datetime.fromisoformat(str(item.get(key) or "").replace("Z", "+00:00")).astimezone(TZ)
        except ValueError:
            continue
    return datetime.now(TZ)


def to_row(item: dict, rates) -> dict:
    amount, currency = parse_amount(item.get("amount", ""))
    when = _when(item)
    card, merchant = str(item.get("card") or ""), str(item.get("merchant") or "?").strip()
    merchant = re.sub(r"^(.{3,}?)\1+$", r"\1", merchant)  # iOS sometimes repeats the name: "AldiAldiAldi"
    key = f"{card}|{item.get('time') or item.get('received')}|{merchant}|{item.get('amount')}"
    return {
        "id": "wallet-" + hashlib.sha1(key.encode()).hexdigest()[:12],
        "date": when.strftime("%Y-%m-%d %H:%M"),
        "amount": float(-amount),
        "currency": currency,
        "amount_pln": float(-rates.to_pln(amount, currency, when.date())),
        "account": _account(card),
        "merchant": merchant,
        "description": f"Apple Pay: {card}",
        "source": "wallet",
    }


def pull(store, rates) -> tuple[int, list[str]]:
    """Moves inbox rows into transactions; returns (saved, unreadable payments to report)."""
    items = store.inbox_rows()
    if not items:
        return 0, []
    added, bad = 0, []
    for item in items:
        try:
            row = to_row(item, rates)
        except (ValueError, LookupError):  # LookupError: NBP has no rate for that currency
            log.warning("unreadable Wallet payment: %s", item)
            bad.append(f"{item.get('merchant') or '?'} {item.get('amount')} ({item.get('card') or '?'})")
            continue
        added += store.add(row)
    store.clear_inbox(len(items))  # only after every row is saved; a crash before this re-reads them
    return added, bad
