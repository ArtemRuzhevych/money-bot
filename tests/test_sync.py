import pytest

from money_bot import sync
from money_bot.sync import MonoSync

ACC_A = {"id": "a", "currencyCode": 980, "maskedPan": ["****1111"]}
ACC_B = {"id": "b", "currencyCode": 980, "maskedPan": ["****2222"]}
T0 = 1_800_000_000


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class FakeApi:
    def __init__(self, accounts, pages=None):
        self.accounts = accounts
        self.pages = list(pages or [])
        self.calls = []

    def card_accounts(self):
        self.calls.append(("accounts",))
        return self.accounts

    def statement(self, account_id, start, end):
        self.calls.append(("statement", account_id, start, end))
        return self.pages.pop(0) if self.pages else []


class FakeStore:
    def __init__(self, state=None):
        self.kv = dict(state or {})
        self.added = []

    def state(self, key):
        return self.kv.get(key)

    def set_state(self, key, value):
        self.kv[key] = value

    def add(self, row):
        self.added.append(row)
        return True


class FakeRates:
    fail = False

    def to_pln(self, amount, code, day):
        if self.fail:
            raise LookupError("no NBP rate")
        return amount


def item(n, t):
    return {
        "id": str(n),
        "time": t,
        "description": "shop",
        "amount": -100,
        "operationAmount": -100,
        "currencyCode": 980,
    }


def make(accounts, pages=None, state=None):
    clock, api, store = Clock(), FakeApi(accounts, pages), FakeStore(state)
    return MonoSync(api, store, FakeRates(), clock), clock, api, store


def test_respects_one_call_per_61_seconds():
    mono, clock, api, _ = make([ACC_A], state={"mono:a": T0 - 600})
    mono.tick()
    clock.now += 60
    mono.tick()
    assert api.calls == [("accounts",)]
    clock.now += 1
    mono.tick()
    assert api.calls[1][0] == "statement"


def test_first_sync_starts_now_without_a_call():
    mono, clock, api, store = make([ACC_A])
    mono.tick()  # accounts
    clock.now += 61
    mono.tick()
    assert store.kv["mono:a"] == T0 + 61
    assert api.calls == [("accounts",)]


def test_syncs_from_cursor_with_overlap_and_advances():
    mono, clock, api, store = make([ACC_A], pages=[[item(1, T0 + 30)]], state={"mono:a": T0 - 600})
    mono.tick()
    clock.now += 61
    mono.tick()

    assert api.calls[1] == ("statement", "a", T0 - 600 - 3600, T0 + 61)
    assert store.added[0]["id"] == "mono-1"
    assert store.kv["mono:a"] == T0 + 61


def test_cycles_accounts_then_waits_ten_minutes():
    mono, clock, api, _ = make([ACC_A, ACC_B], state={"mono:a": T0, "mono:b": T0})
    for _ in range(4):
        mono.tick()
        clock.now += 61
    assert [c[1] for c in api.calls if c[0] == "statement"] == ["a", "b"]  # third tick waits for the cycle

    clock.now = T0 + 61 + sync.CYCLE
    mono.tick()
    assert api.calls[-1][1] == "a"


def test_full_page_continues_from_oldest_item_before_moving_on():
    full = [item(n, T0 - n) for n in range(sync.PAGE)]
    mono, clock, api, store = make(
        [ACC_A, ACC_B], pages=[full, [item(999, T0 - 9000)]], state={"mono:a": T0 - 7200}
    )
    mono.tick()
    clock.now += 61
    mono.tick()
    assert "mono:a" in store.kv and store.kv["mono:a"] == T0 - 7200  # not advanced mid-page
    clock.now += 61
    mono.tick()

    assert api.calls[2] == ("statement", "a", T0 - 7200 - 3600, T0 - (sync.PAGE - 1))
    assert store.kv["mono:a"] == T0 + 61  # the top of the first page
    assert len(store.added) == sync.PAGE + 1


def test_long_gap_catches_up_in_31_day_windows():
    old = T0 - 40 * 86400
    mono, clock, api, store = make([ACC_A], state={"mono:a": old})
    mono.tick()
    clock.now += 61
    mono.tick()
    start = old - 3600
    assert api.calls[1] == ("statement", "a", start, start + sync.MAX_RANGE)
    assert store.kv["mono:a"] == start + sync.MAX_RANGE

    clock.now += 61
    mono.tick()  # same account again, without waiting for the cycle
    assert api.calls[2][1:3] == ("a", start + sync.MAX_RANGE - 3600)


def test_failure_keeps_cursor_and_skips_account_until_next_cycle():
    mono, clock, api, store = make(
        [ACC_A, ACC_B], pages=[[item(1, T0)]], state={"mono:a": T0 - 600, "mono:b": T0}
    )
    mono.rates.fail = True
    mono.tick()
    clock.now += 61
    with pytest.raises(LookupError):
        mono.tick()
    assert store.kv["mono:a"] == T0 - 600

    mono.rates.fail = False
    clock.now += 61
    mono.tick()
    assert api.calls[-1][1] == "b"


def test_refreshes_accounts_daily():
    mono, clock, api, _ = make([ACC_A], state={"mono:a": T0})
    mono.tick()
    clock.now += sync.ACCOUNTS_TTL + 1
    mono.tick()
    assert api.calls.count(("accounts",)) == 2
