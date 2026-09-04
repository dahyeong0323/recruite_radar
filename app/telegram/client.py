from __future__ import annotations

from typing import Any

import httpx
from app.utils.security import safe_exception


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=15.0)
        self._external_client = client is not None
        self.base_url = f"https://api.telegram.org/bot{token}"

    async def close(self) -> None:
        if not self._external_client:
            await self.client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.post(f"{self.base_url}/{method}", json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise TelegramError(data.get("description", "Telegram API error"))
            return data
        except Exception as error:
            raise TelegramError(safe_exception(f"Telegram {method}", error, (self.token,))) from error

    async def send_message(self, chat_id: str, text: str, *, reply_markup: dict | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str) -> dict[str, Any]:
        return await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
