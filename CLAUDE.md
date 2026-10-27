# money-bot

Telegram finance assistant backed by a Google Sheet. Overview in `README.md`, setup in `docs/setup.md`, design in `docs/architecture.md`.

## Working rules

- Checks before every push: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`.
- Tests never touch the network: fake Telegram, Sheets, monobank, NBP and Claude the way the existing tests do.
- Keep `requirements.txt` in sync with `uv.lock`: `uv export --no-dev --no-hashes --no-annotate --frozen -o requirements.txt` (CI checks it).
- Never handle secrets: they live only in the host's `.env`. Changing `.env` needs a restart of the host.
- `launcher.py` runs on the host from the server root and doesn't update itself; changing it means uploading it again.
- No personal data in the repo: no real names, card numbers, account balances or transactions in code, tests or docs.
