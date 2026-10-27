# Setup

Everything secret goes into one `.env` file on the host, never into the repo. [`.env.example`](../.env.example) lists every setting.

## 1. Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot` and follow the steps. Keep the token: `TELEGRAM_BOT_TOKEN`.
2. Find your numeric user ID, for example by messaging [@userinfobot](https://t.me/userinfobot): `TELEGRAM_USER_ID`. The bot answers only this ID and logs anyone else once.

## 2. Google Sheet and service account

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Google Sheets API**.
2. **IAM & Admin → Service accounts → Create service account.** No roles are needed. Open it, **Keys → Add key → JSON**, and download the file.
3. Create an empty Google Sheet. Share it with the service account's email (it ends in `iam.gserviceaccount.com`) as **Editor**.
4. The Sheet's ID is the long part of its URL between `/d/` and `/edit`: `SHEET_ID`.

On the first start the bot creates its tabs (`transactions`, `budget`, `state`, `categories`, `rules`, `receipts`, `receipt_items`, `inbox`) and seeds the categories and starter rules. It never overwrites a tab you've edited.

## 3. Claude (optional, but most of the fun)

Receipts, category guesses, `/ask` and the Sunday remark use Claude Code in print mode with your Claude subscription.

1. On your own computer, with Claude Code installed, run `claude setup-token` and log in.
2. Put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.

The bot downloads the official Claude Code binary on start, checks its SHA-256 against the release manifest and keeps it in `~/.money-bot/`. Claude is only allowed the Read tool and runs in a temporary folder that holds just the file it needs. Without the token everything else works and these features say they're off.

## 4. Host and deploy

The host only needs Python 3.13 and outbound internet; it never accepts incoming connections.

1. Fork or copy this repo to your GitHub account.
2. For a private repo, create a fine-grained token with access to that repo only and **Contents: Read-only**: `GITHUB_TOKEN`. A public repo needs no token.
3. Upload `launcher.py` to the server root and make it the startup file.
4. Create `.env` next to it from `.env.example`: at least `GITHUB_REPO`, the Telegram settings, `SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_FILE`. Upload the service account JSON to the path you gave there.
5. Start the server. The launcher downloads the `deploy` branch into `app/`, installs `requirements.txt` and starts the bot. The console shows the commit it runs.

From then on, every push to `main` that passes CI moves `deploy`, and the launcher updates within `POLL_SECONDS` (default 300). It also restarts the bot if it crashes. Logs go to the console and to `logs/` in the server root.

Two things to remember:
- The launcher reads `.env` only when it starts. Restart the server after changing it.
- `launcher.py` doesn't update itself. If it changes, upload it again.

If something required is missing, the bot logs which settings and waits instead of crash-looping.

## 5. monobank (optional)

1. Get a personal token at [api.monobank.ua](https://api.monobank.ua/) and put it in `.env` as `MONOBANK_TOKEN`.
2. Restart. The first sync only marks the starting point; payments from then on arrive within about 10 minutes, converted to PLN.

monobank allows one call per minute, so the bot spaces its calls and catches up after any downtime.

## 6. Apple Pay feed (optional, iPhone)

Every Apple Pay payment with the cards you choose reaches the Sheet within a minute. The iPhone sends it to a small Google Apps Script attached to the Sheet (the host can't receive connections), the script adds a row to the `inbox` tab, and the bot moves it into `transactions` every minute.

### The script

1. In the Sheet: **Extensions → Apps Script**.
2. Replace everything in `Code.gs` with [`apps_script/inbox.gs`](../apps_script/inbox.gs). Save.
3. **Project Settings → Script Properties → Add script property:** `SECRET`, with a long random value you make up (30 or more letters and digits).
4. **Deploy → New deployment → Web app.** Execute as **Me**, Who has access **Anyone**. Deploy, allow access, and copy the **Web app URL** (it ends in `/exec`).

"Anyone" is required: the iPhone calls the URL without a Google login. Without the secret the script answers `forbidden` and writes nothing. To update the code later, use **Manage deployments → edit → New version** so the URL stays the same.

### The automation

1. **Shortcuts → Automation → + → Transaction.** Pick the cards to track and leave categories and merchants on any. Make it run automatically (Run Immediately, or the Automation toggle on newer iOS).
2. Add **Get Contents of URL** with the Web app URL. **Method POST**, **Request Body JSON**, and five Text fields:
   - `secret`: the same value as in Script Properties
   - `card`: Shortcut Input → **Card or Pass**
   - `merchant`: Shortcut Input → **Merchant**
   - `amount`: Shortcut Input → **Amount**
   - `time`: **Current Date**, Date Format **ISO 8601** with time

All five go in the request body; the script can't read headers.

### Accounts

The card's Wallet name becomes the account, unless `CARD_ACCOUNTS` maps it. `CARD_ACCOUNTS=mbank=mbank,revolut=revolut` saves a card named "mBank Visa" as `mbank`. Leave cards that already sync another way (like monobank) out of the automation, or they'll be counted twice.

### Checking it

Run the shortcut by hand once: a row with empty card and amount should appear in `inbox`. Shortcuts shows a Google Drive error page after the call; that's Google's redirect and doesn't mean it failed. In Apps Script, **Executions** lists each call as type "Web app". After that, pay for something small with Apple Pay.

Not everything goes through Apple Pay: BLIK, transfers, incoming money and card-number payments online need to be typed in. The Wallet trigger can also occasionally miss a payment.
