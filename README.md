# money-bot

A personal finance assistant that lives in one Telegram chat and keeps everything in one Google Sheet.

- **Cash and BLIK in one line:** send `kebab 32` or `blik 50 allegro` and it's logged.
- **Card payments by themselves:** every Apple Pay payment reaches the Sheet within a minute through an iOS Shortcuts automation. monobank accounts sync through monobank's personal API.
- **Receipt photos:** send a photo and Claude reads every line, links the receipt to the card payment and splits it across categories.
- **Categories that learn:** rules sort known shops; for new ones the bot asks once at 21:00 with one-tap buttons (Claude's guess first), and your answer becomes a rule.
- **A weekly budget:** `/week` shows what's left. A 21:00 digest on days you spent money, a Sunday summary with a short comment from Claude.
- **Questions:** `/ask how much did I spend on groceries this month?` is answered by Claude from your real rows.

It runs on a small Python host (built for a Pterodactyl panel with no shell, like Daki) and uses Claude Code with your own Claude subscription, so there are no per-token API costs.

## What it assumes

- **Poland:** amounts are kept in PLN, foreign currencies are converted with the NBP mid rate, weeks run Monday to Sunday in `Europe/Warsaw` (change with `TIMEZONE`), and the starter category rules know Polish shops. Other countries work, but you'd want to adjust those.
- **One user:** only your Telegram user ID gets answers.
- **iPhone for card payments:** the Apple Pay feed uses iOS Shortcuts. Without it you can still type payments in.

## Commands

| You send | The bot |
|---|---|
| `kebab 32`, `32 kebab`, `kebab 32,50` | Logs a cash payment and shows what's left this week |
| `blik 50 allegro` | Logs a BLIK payment under `BLIK_ACCOUNT` |
| `+200 gift` | Logs income |
| a receipt photo (or an image file) | Reads it, links it to the payment or asks "Cash" / "Card, link later" |
| `/week` | This week's spending, top shops and categories |
| `/limit 600` | Changes the weekly limit (`/limit` alone shows it) |
| `/categorize` | Asks about shops without a category now |
| `/ask <question>` | Answers from your data (needs Claude) |

## Setup

[docs/setup.md](docs/setup.md) walks through it: a Telegram bot, a Google service account and Sheet, a Claude token, the host and the deploy pipeline, and optionally monobank and the Apple Pay feed. Plan on about an hour the first time.

How the pieces fit: [docs/architecture.md](docs/architecture.md).

## Develop

```
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Tests run without network: Telegram, Google Sheets, monobank, NBP and Claude are all faked.

Pushing to `main` runs the checks in CI and then moves the `deploy` branch; the launcher on the host picks it up within 5 minutes.

## License

MIT, see [LICENSE](LICENSE).
