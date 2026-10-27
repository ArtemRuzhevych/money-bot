# Architecture

One Python process, no web server, no database. The Google Sheet is the database and the dashboard.

```
 iPhone (Apple Pay) ──POST──> Apps Script ──> Sheet "inbox" ──┐
 monobank API <──────────── pulled every 10 min ──────────────┤
 Telegram <──── long poll ────> money_bot (one loop) ─────────┼──> Google Sheet
 NBP rates <── on demand ──┘          │                       │
                                      └── claude -p (receipts, guesses, /ask)
```

## The loop

`money_bot/__main__.py` runs one loop with no threads:

1. Long-poll Telegram for 30 s and handle each update (messages, photos, button presses).
2. Once a day after 21:00: the digest of today's payments and the category questions.
3. Sunday after 20:00: the weekly summary.
4. Every minute: move Apple Pay payments from `inbox` to `transactions`.
5. If monobank is on: at most one monobank call, when one is due.

Every scheduled job saves "done for today/this week" in the `state` tab before it sends, so a crash or restart can't send it twice. A failing job is logged and the loop goes on.

## Modules

| Module | Does |
|---|---|
| `handlers.py` | Text in, reply out: cash parsing, `/week`, `/limit`, routing. Pure, so it tests without network |
| `store.py` | The Sheet: creates tabs once, appends with duplicate IDs skipped, rules, receipts, state |
| `telegram.py` | Raw Bot API over `requests`; errors never include the token (it's in the URL) |
| `categories.py` | Accent-insensitive matching (longest rule wins), starter rules, the question flow |
| `receipts.py` | Claude reads a photo into items; matching a receipt to a payment; cash or card-later |
| `insights.py` | Digest, Sunday summary, `/ask` snapshot and prompts |
| `wallet.py` | Apple Pay inbox rows into transactions: amount formats, card to account |
| `monobank.py`, `sync.py` | monobank client and the paced, cursor-based sync |
| `fx.py` | NBP mid rates, the last published rate on or before the day |
| `claude.py` | Installs the Claude Code binary (checksum verified) and runs `claude -p` |
| `launcher.py` | On the host: polls the `deploy` branch, installs, runs and restarts the bot |

## Data

`transactions` columns: `id, date, amount, currency, amount_pln, account, merchant, description, category, source, receipt_id, note`.

IDs make every source safe to repeat: `tg-<message id>` for typed entries, `mono-<id>` from monobank, `wallet-<hash>` for Apple Pay, `rcpt-<hash>` for receipts. A re-delivered Telegram message, a retried POST or an overlapping monobank window adds nothing twice.

Other tabs: `budget` (weekly limit in B1), `state` (sync cursors and "last sent" markers), `categories`, `rules` (pattern to category, editable), `receipts` and `receipt_items`, `inbox` (Apple Pay rows waiting to be moved).

## Claude

Claude Code runs as `claude -p --output-format json --allowedTools Read` in a fresh temporary folder that holds only what the call needs: the receipt image, or a CSV snapshot of the last 12 months for `/ask` (without descriptions or notes). The folder is deleted afterwards. Haiku reads receipts and guesses categories; Sonnet answers `/ask` and writes the Sunday remark. If Claude fails, the feature degrades: questions come without a guess, the summary goes out without the remark.

## Why it's built this way

- **Pull, not push:** the host accepts no incoming connections, so Telegram is long-polled, monobank is polled, Apple Pay goes through Apps Script into the Sheet, and deploys are pulled by the launcher from a branch.
- **The Sheet as the store:** nothing to back up or migrate, and every number can be checked and fixed by hand.
- **Claude Code on a subscription:** the AI parts cost nothing beyond a subscription you may already have, at the price of a 280 MB binary on the host.
- **No frameworks:** the Telegram Bot API needs four methods; `requests` covers them.
