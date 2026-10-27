"""Minimal Telegram Bot API client: long-poll updates and send text."""

from pathlib import Path

import requests

POLL_SECONDS = 30


class Telegram:
    def __init__(self, token: str, session=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = session or requests.Session()
        self.offset = 0

    def _call(self, method: str, **params):
        try:
            response = self.session.post(self.base + method, json=params, timeout=POLL_SECONDS + 15)
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            # requests puts the URL, and so the bot token, in its messages: log only the error type.
            raise RuntimeError(f"telegram {method}: {type(e).__name__}") from None
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def updates(self) -> list[dict]:
        result = self._call(
            "getUpdates",
            offset=self.offset,
            timeout=POLL_SECONDS,
            allowed_updates=["message", "callback_query"],
        )
        if result:
            self.offset = result[-1]["update_id"] + 1  # acknowledges them on the next call
        return result

    def send(self, chat_id: int, text: str, keyboard: list | None = None) -> None:
        params: dict = {"chat_id": chat_id, "text": text}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        self._call("sendMessage", **params)

    def answer_callback(self, callback_id: str) -> None:
        """Stops the button's loading spinner in Telegram."""
        self._call("answerCallbackQuery", callback_query_id=callback_id)

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)

    def download(self, file_id: str) -> tuple[bytes, str]:
        """A file the owner sent: its bytes and extension (Bot API files are up to 20 MB)."""
        path = self._call("getFile", file_id=file_id)["file_path"]
        url = self.base.replace("/bot", "/file/bot", 1) + path
        try:
            response = self.session.get(url, timeout=60)
        except requests.RequestException as e:
            raise RuntimeError(f"telegram download: {type(e).__name__}") from None  # the URL holds the token
        if response.status_code != 200:
            raise RuntimeError(f"telegram download: HTTP {response.status_code}")
        return response.content, Path(path).suffix or ".jpg"
