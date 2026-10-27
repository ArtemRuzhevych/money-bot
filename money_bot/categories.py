"""Categories: rule matching, Polish starter rules and the questions about new shops."""

import json
import logging
import re
import tempfile
import unicodedata
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from money_bot import claude
from money_bot.handlers import TZ

ASK_HOUR = 21
MAX_QUESTIONS = 8
EXPIRED = "This question expired, send /categorize"
DEFAULT_CATEGORIES = [
    "Groceries",
    "Eating out",
    "Transport",
    "Household",
    "Health",
    "Subscriptions",
    "Fun",
    "Clothes",
    "Gifts",
    "Debt payment",
    "Income",
    "Transfer",
    "Other",
]
# Common Polish shops. Allegro and Amazon sell everything, so they're left to ask.
STARTER = {
    "Groceries": [
        "biedronka", "lidl", "zabka", "carrefour", "auchan", "kaufland", "netto", "dino",
        "stokrotka", "lewiatan", "aldi", "polomarket", "intermarche", "frisco",
    ],
    "Eating out": [
        "mcdonald", "kfc", "burger king", "kebab", "pizza", "starbucks", "costa coffee",
        "pyszne", "glovo", "wolt", "uber eats", "bolt food",
    ],
    "Transport": [
        "uber", "bolt", "freenow", "jakdojade", "mpk", "ztm", "pkp", "intercity", "koleo",
        "orlen", "circle k", "shell",
    ],
    "Household": ["rossmann", "hebe", "pepco", "action", "ikea", "castorama", "leroy merlin", "jysk"],
    "Health": ["apteka", "doz", "superpharm", "medicover", "luxmed"],
    "Subscriptions": [
        "netflix", "spotify", "youtube", "anthropic", "claude.ai", "apple.com", "google", "disney",
    ],
    "Clothes": ["zalando", "reserved", "sinsay", "cropp", "house", "h&m", "zara", "decathlon"],
    "Fun": ["empik", "cinema city", "multikino", "helios", "steam", "playstation"],
}  # fmt: skip
log = logging.getLogger(__name__)


def starter_rules() -> list[tuple[str, str]]:
    return [(pattern, category) for category, patterns in STARTER.items() for pattern in patterns]


def fold(text: str) -> str:
    """Lowercase without accents, so 'Żabka' and 'zabka' match. NFKD leaves ł alone, hence the map."""
    text = unicodedata.normalize("NFKD", str(text).replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def match(merchant: str, rules: list[tuple[str, str]]) -> str | None:
    """Longest pattern that starts at a word boundary in the merchant name."""
    name = fold(merchant)
    hits = [(len(p), c) for p, c in rules if p and re.search(r"(?<!\w)" + re.escape(fold(p)), name)]
    return max(hits)[1] if hits else None


def _amount(value) -> Decimal | None:
    return None if value in ("", None) else Decimal(str(value).replace(",", "."))


def pick(row: dict, rules: list[tuple[str, str]]) -> str | None:
    found = match(row.get("merchant", ""), rules)
    if found is None and (_amount(row.get("amount_pln")) or 0) > 0:
        return "Income"
    return found


def pending(rows: list[dict]) -> list[tuple[str, int, Decimal]]:
    """Uncategorised shops, oldest first: (merchant, payments, total PLN)."""
    found: dict[str, list] = {}
    for row in rows:
        amount = _amount(row.get("amount_pln"))
        if row.get("category") or amount is None:
            continue
        entry = found.setdefault(row.get("merchant") or "?", [0, Decimal(0)])
        entry[0] += 1
        entry[1] += amount
    return [(m, n, total) for m, (n, total) in found.items()][:MAX_QUESTIONS]


def parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def claude_guess(merchants: list[str], cats: list[str], ask=claude.ask) -> dict[str, str]:
    """Claude Haiku's category for each shop name; anything off-list is dropped, failures give {}."""
    prompt = (
        "These are shop names from a bank statement of a student living in Poland. "
        f"Pick the most likely category for each from this list: {json.dumps(cats)}. "
        "Reply with only a JSON object mapping each name exactly as given to one category.\n"
        f"Names: {json.dumps(merchants, ensure_ascii=False)}"
    )
    try:
        data = parse_json(ask(prompt, cwd=Path(tempfile.gettempdir()), model="haiku", timeout=90)["result"])
    except Exception as e:
        log.warning("category guess failed: %s", e)
        return {}
    return {m: c for m, c in data.items() if m in merchants and c in cats}


def ask_due(now: datetime, store) -> bool:
    local = now.astimezone(TZ)
    return local.hour >= ASK_HOUR and store.state("ask:last") != local.date().isoformat()


class Asker:
    """Builds the questions and remembers them until answered (in memory: a restart expires them)."""

    def __init__(self, guess: Callable[[list[str], list[str]], dict[str, str]] | None = None):
        self.guess = guess
        self.open: dict[int, tuple[str, list[str]]] = {}
        self.next_id = 1

    def ask(self, rows: list[dict], cats: list[str]) -> list[tuple[str, list[list[dict]]]]:
        shops = pending(rows)
        guesses: dict[str, str] = {}
        if self.guess and shops:
            try:
                guesses = self.guess([m for m, _, _ in shops], cats)
            except Exception as e:
                log.warning("category guess failed: %s", e)
        questions = []
        for merchant, count, total in shops:
            qid, self.next_id = self.next_id, self.next_id + 1
            guess = guesses.get(merchant)
            order = ([guess] if guess else []) + [c for c in cats if c != guess]
            self.open[qid] = (merchant, order)
            buttons = [
                {"text": f"★ {c}" if guess and i == 0 else c, "callback_data": f"c:{qid}:{i}"}
                for i, c in enumerate(order)
            ]
            buttons.append({"text": "Skip", "callback_data": f"c:{qid}:skip"})
            text = f"{merchant}: {total:.2f} PLN ({count} payment{'s' if count > 1 else ''}). Category?"
            questions.append((text, [buttons[i : i + 2] for i in range(0, len(buttons), 2)]))
        return questions

    def answer(self, data: str, store) -> str:
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "c" or not parts[1].isdigit() or int(parts[1]) not in self.open:
            return EXPIRED
        merchant, order = self.open[int(parts[1])]
        if parts[2] == "skip":
            del self.open[int(parts[1])]
            return f"{merchant}: skipped"
        if not parts[2].isdigit() or int(parts[2]) >= len(order):
            return EXPIRED
        del self.open[int(parts[1])]
        category = order[int(parts[2])]
        store.add_rule(merchant.lower(), category)
        updated = store.recategorize()
        return f"{merchant} → {category} ✓ ({updated} payments updated)"
