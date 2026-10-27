"""monobank sync, driven from the main loop between Telegram polls (no threads)."""

import logging
import time

from money_bot.monobank import to_row

CALL_GAP = 61  # monobank allows one personal API call per 60 s
CYCLE = 600  # sync every account every 10 minutes
ACCOUNTS_TTL = 86400  # refresh the account list daily
OVERLAP = 3600  # re-read the last hour for late postings; duplicate IDs are skipped
MAX_RANGE = 31 * 86400  # longest statement window monobank serves
PAGE = 500  # a full page means there may be older items in the window

log = logging.getLogger(__name__)


class MonoSync:
    def __init__(self, api, store, rates, clock=time.time):
        self.api, self.store, self.rates, self.clock = api, store, rates, clock
        self.accounts: dict[str, dict] = {}
        self.accounts_at: float | None = None
        self.queue: list[str] = []
        self.pages: dict[str, tuple[int, int]] = {}  # account -> (window top, next page end)
        self.next_call = 0.0
        self.next_cycle = 0.0

    def tick(self) -> None:
        """Do at most one monobank call if one is due."""
        now = self.clock()
        if now < self.next_call:
            return
        if self.accounts_at is None or now - self.accounts_at > ACCOUNTS_TTL:
            self.next_call = now + CALL_GAP
            self.accounts = {a["id"]: a for a in self.api.card_accounts()}
            self.accounts_at = now
            log.info("monobank: %d card accounts", len(self.accounts))
            return
        if not self.queue:
            if now < self.next_cycle:
                return
            self.queue = list(self.accounts)
            self.next_cycle = now + CYCLE
        if not self.queue:
            return
        account_id = self.queue[0]
        try:
            done = self._sync(self.accounts[account_id], int(now))
        except Exception:
            self.pages.pop(account_id, None)
            self.queue.pop(0)  # retried next cycle; the cursor didn't move
            raise
        if done:
            self.queue.pop(0)

    def _sync(self, account: dict, now: int) -> bool:
        """Sync one window or page; True when the account is caught up."""
        key = f"mono:{account['id']}"
        cursor = self.store.state(key)
        if cursor is None:
            self.store.set_state(key, now)  # no backfill: history starts at the first sync
            return True
        start = int(cursor) - OVERLAP
        top, end = self.pages.get(account["id"], (None, None))
        if top is None:
            top = end = min(now, start + MAX_RANGE)
        self.next_call = now + CALL_GAP
        items = self.api.statement(account["id"], start, end)
        added = sum(self.store.add(to_row(item, account, self.rates)) for item in items)
        if added:
            log.info("monobank: %d new transactions on %s", added, account["id"][-4:])
        if len(items) >= PAGE:
            self.pages[account["id"]] = (top, min(i["time"] for i in items))
            return False
        self.pages.pop(account["id"], None)
        self.store.set_state(key, top)  # only after every row in the window is saved
        return top >= now
