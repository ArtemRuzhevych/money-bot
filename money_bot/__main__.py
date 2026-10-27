"""Entry point: poll Telegram, answer the owner, write to the Sheet."""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from launcher import setup_logging  # launcher.py ships inside app/ alongside the bot
from money_bot import categories, claude, handlers, insights, wallet
from money_bot.categories import Asker
from money_bot.fx import Rates
from money_bot.monobank import Monobank
from money_bot.receipts import ReceiptDesk
from money_bot.store import Store
from money_bot.sync import MonoSync
from money_bot.telegram import Telegram

WALLET_EVERY = 60  # seconds between reads of the inbox tab
REQUIRED = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID", "SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE"]
log = logging.getLogger("money_bot")


def idle(reason: str) -> NoReturn:
    """Wait for a fix instead of exiting, so the launcher doesn't restart into the same error every 10 s."""
    log.error("%s; idling until the next deploy or restart", reason)
    while True:
        time.sleep(3600)


def deliver(bot, reply: handlers.Reply) -> None:
    try:
        if reply.callback_id:
            bot.answer_callback(reply.callback_id)
        if reply.edit_message_id:
            bot.edit(reply.chat_id, reply.edit_message_id, reply.text)
        else:
            bot.send(reply.chat_id, reply.text, reply.keyboard)
    except Exception as e:
        log.warning("reply failed: %s", e)


def handle_updates(
    bot, updates: list[dict], store, allowed_id: int, ignored: set, asker, desk, oracle=None
) -> None:
    for update in updates:
        source = update.get("message") or update.get("callback_query") or {}
        try:
            replies = handlers.process(
                update, allowed_id, store, datetime.now(handlers.TZ), asker, desk, oracle
            )
        except Exception:
            log.exception("handling update %s failed", update.get("update_id"))
            chat = (source.get("chat") or source["message"]["chat"])["id"]
            replies = [handlers.Reply(chat, "Something went wrong, it's in the log.")]
        if replies is None:
            sender = source.get("from", {}).get("id")
            if sender not in ignored:
                ignored.add(sender)
                log.warning("ignoring messages from user %s", sender)
            continue
        for reply in replies:
            deliver(bot, reply)


def claude_ready() -> bool:
    """Claude Code installed and logged in: needed for receipts and category guesses."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        log.info("CLAUDE_CODE_OAUTH_TOKEN not set: no receipts, category questions come without guesses")
        return False
    try:
        claude.install()
    except Exception:
        log.exception("installing Claude Code failed: no receipts, category questions come without guesses")
        return False
    return True


def make_asker(ready: bool) -> Asker:
    return Asker(guess=categories.claude_guess if ready else None)


def make_desk(bot, ready: bool) -> ReceiptDesk:
    return ReceiptDesk(download=bot.download, ask=claude.ask if ready else None)


def make_oracle(bot, ready: bool) -> insights.Oracle | None:
    if not ready:
        return None
    return insights.Oracle(send=lambda chat, text: deliver(bot, handlers.Reply(chat, text)))


def maybe_ask(bot, store, asker, allowed_id: int, now: datetime) -> None:
    """The 21:00 run, once a day: today's digest and questions about new shops, each only if any."""
    if not categories.ask_due(now, store):
        return
    store.set_state(
        "ask:last", now.astimezone(handlers.TZ).date().isoformat()
    )  # first, so a failure can't repeat it
    rows = store.rows()
    digest = insights.digest(rows, store.weekly_limit(), now, store.receipt_splits())
    if digest:
        deliver(bot, handlers.Reply(allowed_id, digest))
    for text, keyboard in asker.ask(rows, store.categories()):
        deliver(bot, handlers.Reply(allowed_id, text, keyboard))  # a private chat's ID is the user's ID


def maybe_summarize(bot, store, allowed_id: int, now: datetime, ask) -> None:
    """The Sunday 20:00 summary, once per week."""
    if not insights.summary_due(now, store):
        return
    store.set_state("summary:last", handlers.week_start(now).date().isoformat())  # first, never twice
    text = insights.summary(store.rows(), store.weekly_limit(), now, store.receipt_splits(), ask=ask)
    deliver(bot, handlers.Reply(allowed_id, text))


def pull_wallet(bot, store, rates, allowed_id: int) -> None:
    added, bad = wallet.pull(store, rates)
    if added:
        log.info("wallet: %d new payments", added)
    for payment in bad:
        text = f"Couldn't read an Apple Pay payment: {payment}. Type it in, like: kiosk 12"
        deliver(bot, handlers.Reply(allowed_id, text))


def make_mono(store) -> MonoSync | None:
    token = os.environ.get("MONOBANK_TOKEN")
    if not token:
        log.info("MONOBANK_TOKEN not set, monobank sync is off")
        return None
    return MonoSync(Monobank(token), store, Rates())


def main() -> NoReturn:
    setup_logging("bot")
    sha_file = Path(".sha")
    log.info("money-bot %s starting", sha_file.read_text().strip()[:7] if sha_file.exists() else "dev")

    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        idle(f"missing in .env: {', '.join(missing)}")
    try:
        store = Store.open(os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], os.environ["SHEET_ID"])
    except Exception:
        log.exception("can't open the Sheet")
        idle("check SHEET_ID, the service account file and that the Sheet is shared with it")
    bot = Telegram(os.environ["TELEGRAM_BOT_TOKEN"])
    allowed_id = int(os.environ["TELEGRAM_USER_ID"])
    log.info("ready, answering user %s", allowed_id)
    mono = make_mono(store)
    ready = claude_ready()
    asker = make_asker(ready)
    desk = make_desk(bot, ready)
    oracle = make_oracle(bot, ready)
    try:
        log.info("categorised %d existing transactions", store.recategorize())
    except Exception:
        log.exception("categorising existing transactions failed")

    rates = Rates()
    next_wallet = 0.0
    ignored: set = set()
    failures = 0
    while True:
        try:
            updates = bot.updates()
            failures = 0
        except Exception as e:
            failures += 1
            log.warning("polling failed (%s), retry %d", e, failures)
            time.sleep(min(60, 2**failures))
            continue
        handle_updates(bot, updates, store, allowed_id, ignored, asker, desk, oracle)
        try:
            maybe_ask(bot, store, asker, allowed_id, datetime.now(handlers.TZ))
        except Exception:
            log.exception("daily digest or category questions failed")
        try:
            maybe_summarize(bot, store, allowed_id, datetime.now(handlers.TZ), claude.ask if ready else None)
        except Exception:
            log.exception("Sunday summary failed")
        if time.monotonic() >= next_wallet:
            next_wallet = time.monotonic() + WALLET_EVERY
            try:
                pull_wallet(bot, store, rates, allowed_id)
            except Exception:
                log.exception("reading Apple Pay payments failed")
        if mono:
            try:
                mono.tick()  # polls return within 30 s, so this runs often enough for its 61 s pacing
            except Exception:
                log.exception("monobank sync failed")


if __name__ == "__main__":
    main()
