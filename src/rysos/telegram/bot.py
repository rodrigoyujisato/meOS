"""Async Telegram Bot client for rysOS using httpx."""

import asyncio
import html
import logging
import re
from typing import Any, Optional, Callable, Awaitable
import httpx

from rysos.config import settings

logger = logging.getLogger("rysos.telegram")

# --- Outbound text formatting -------------------------------------------------
# Messages are sent with parse_mode=HTML, so Markdown emphasis the LLM emits
# (**bold**, `#` headings, `-`/`*` bullets) shows up as literal characters on
# screen. We normalise the handful of markers the model actually produces into
# the small HTML subset Telegram renders, and escape bare ampersands and angle
# brackets so a title, e-mail address (e.g. "<name@host>"), or reply containing
# "&"/"<" doesn't 400 the whole message. The app's own strings already use real
# <b>/<i>/<code> tags and pass through untouched. Single * / _ are left alone
# on purpose (snake_case filenames, currency, math).
_BARE_AMP_RE = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});)")
_BARE_LT_RE = re.compile(r"<(?!/?(?:b|i|code)>)")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BOLD_STAR_RE = re.compile(r"(?<!\*)\*\*(?!\s)([^\n*]+?)(?<!\s)\*\*(?!\*)")
_BOLD_US_RE = re.compile(r"(?<!_)__(?!\s)([^\n_]+?)(?<!\s)__(?!_)")
_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+(?=\S)", re.MULTILINE)
_TAG_RE = re.compile(r"<[^>]+>")


def format_for_telegram(text: str) -> str:
    """Converts model-authored Markdown into the HTML subset Telegram accepts."""
    if not text:
        return text
    text = _BARE_AMP_RE.sub("&amp;", text)
    text = _BARE_LT_RE.sub("&lt;", text)
    text = _HEADING_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_STAR_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_US_RE.sub(r"<b>\1</b>", text)
    text = _BULLET_RE.sub(r"\1• ", text)
    return text


def _strip_html_tags(text: str) -> str:
    """Last-resort plain-text rendering when Telegram rejects the HTML payload."""
    return html.unescape(_TAG_RE.sub("", text or ""))


class TelegramBot:
    """Lightweight async Telegram Bot API client for executive communication."""

    def __init__(self, token: Optional[str] = None):
        self._token = token
        self._running = False
        self._polling_task: Optional[asyncio.Task] = None
        self._message_handler: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
        self._callback_handler: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def token(self) -> Optional[str]:
        if self._token is not None:
            return self._token
        return settings.TELEGRAM_BOT_TOKEN

    @property
    def api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    @property
    def http(self) -> httpx.AsyncClient:
        """A single pooled client so every call reuses the TLS connection to Telegram."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120.0),
            )
        return self._client

    async def aclose(self) -> None:
        """Closes the pooled client; call on application shutdown."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def is_configured(self) -> bool:
        """Returns True if a bot token is provided."""
        t = self.token
        return bool(t and len(t) > 10 and ":" in t)

    def set_message_handler(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._message_handler = handler

    def set_callback_handler(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._callback_handler = handler

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> Optional[dict[str, Any]]:
        """Sends a text message to a chat. Falls back to plain text if formatting fails."""
        if not self.is_configured():
            logger.debug("Telegram bot token not configured.")
            return None

        url = f"{self.api_url}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": format_for_telegram(text) if parse_mode == "HTML" else text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = await self.http.post(url, json=payload)
            if resp.status_code == 200:
                return resp.json()

            # If parse_mode caused an error (e.g. malformed HTML tag), fallback to plain text
            if parse_mode and resp.status_code == 400:
                payload.pop("parse_mode", None)
                payload["text"] = _strip_html_tags(payload["text"])
                fallback_resp = await self.http.post(url, json=payload)
                if fallback_resp.status_code == 200:
                    return fallback_resp.json()

            logger.warning(f"Telegram sendMessage failed [{resp.status_code}]: {resp.text}")
            return None
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem Telegram: {e}")
            return None

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: Optional[str] = "HTML",
    ) -> Optional[dict[str, Any]]:
        """Edits an existing message in a chat."""
        if not self.is_configured():
            return None

        url = f"{self.api_url}/editMessageText"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": format_for_telegram(text) if parse_mode == "HTML" else text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            resp = await self.http.post(url, json=payload)
            if resp.status_code == 200:
                return resp.json()
            if parse_mode and resp.status_code == 400:
                payload.pop("parse_mode", None)
                payload["text"] = _strip_html_tags(payload["text"])
                fallback_resp = await self.http.post(url, json=payload)
                if fallback_resp.status_code == 200:
                    return fallback_resp.json()
            logger.warning(f"Telegram editMessageText failed [{resp.status_code}]: {resp.text}")
            return None
        except Exception as e:
            logger.error(f"Erro ao editar mensagem Telegram: {e}")
            return None

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """Answers an inline keyboard callback query."""
        if not self.is_configured():
            return False

        url = f"{self.api_url}/answerCallbackQuery"
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert

        try:
            resp = await self.http.post(url, json=payload, timeout=15.0)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Erro ao responder callback Telegram: {e}")
            return False

    async def send_chat_action(self, chat_id: int | str, action: str = "typing") -> bool:
        """Sends chat action indicator (e.g. typing, upload_document)."""
        if not self.is_configured():
            return False

        url = f"{self.api_url}/sendChatAction"
        payload = {"chat_id": chat_id, "action": action}
        try:
            resp = await self.http.post(url, json=payload, timeout=10.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """Registers the bot's command list so Telegram shows the native "/" menu."""
        if not self.is_configured():
            return False

        url = f"{self.api_url}/setMyCommands"
        try:
            resp = await self.http.post(url, json={"commands": commands}, timeout=10.0)
            ok = resp.status_code == 200 and resp.json().get("ok", False)
            if not ok:
                logger.warning(f"setMyCommands não aceito pelo Telegram: {resp.text[:200]}")
            return bool(ok)
        except Exception as e:
            logger.error(f"Erro ao registrar comandos no Telegram: {e}")
            return False

    async def get_file(self, file_id: str) -> Optional[dict[str, Any]]:
        """Retrieves file info including file_path from Telegram API."""
        if not self.is_configured():
            return None

        url = f"{self.api_url}/getFile"
        try:
            resp = await self.http.get(url, params={"file_id": file_id})
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result")
            return None
        except Exception as e:
            logger.error(f"Erro ao obter getFile no Telegram: {e}")
            return None

    async def download_file_bytes(self, file_path: str) -> Optional[bytes]:
        """Downloads raw bytes of a file from Telegram API."""
        if not self.is_configured():
            return None

        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            resp = await self.http.get(url, timeout=40.0)
            if resp.status_code == 200:
                return resp.content
            return None
        except Exception as e:
            logger.error(f"Erro ao baixar arquivo do Telegram: {e}")
            return None

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> list[dict[str, Any]]:
        """Polls updates from Telegram API via long-polling."""
        if not self.is_configured():
            return []

        url = f"{self.api_url}/getUpdates"
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        try:
            resp = await self.http.get(url, params=params, timeout=float(timeout + 10))
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result", [])
            logger.warning(f"Telegram getUpdates returned {resp.status_code}: {resp.text}")
            return []
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            return []
        except Exception as e:
            logger.warning(f"Erro no long-polling Telegram: {e}")
            return []

    async def start_polling(self) -> None:
        """Starts background long-polling loop."""
        if not self.is_configured():
            logger.info("Telegram Bot não configurado (TELEGRAM_BOT_TOKEN ausente). Polling não iniciado.")
            print("[Telegram Bot] Bot não configurado. Polling não iniciado.")
            return

        self._running = True
        logger.info("Iniciando Telegram Bot long-polling...")
        print("[Telegram Bot] Long-polling iniciado com sucesso! Escutando mensagens...")
        offset: Optional[int] = None

        while self._running:
            try:
                updates = await self.get_updates(offset=offset, timeout=20)
                for update in updates:
                    update_id = update.get("update_id")
                    if update_id is not None:
                        offset = update_id + 1

                    # Message handling
                    if "message" in update and self._message_handler:
                        asyncio.create_task(self._safe_handle(self._message_handler, update["message"]))
                    # Callback query handling (Inline buttons)
                    elif "callback_query" in update and self._callback_handler:
                        asyncio.create_task(self._safe_handle(self._callback_handler, update["callback_query"]))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Exceção no ciclo de polling do Telegram: {e}")
                await asyncio.sleep(3)

        logger.info("Telegram Bot long-polling finalizado.")

    async def _safe_handle(self, handler: Callable[[dict[str, Any]], Awaitable[None]], data: dict[str, Any]) -> None:
        """Executes handler with catch-all exception logging."""
        try:
            await handler(data)
        except Exception as e:
            logger.error(f"Erro no processamento do evento Telegram: {e}", exc_info=True)

    def stop(self) -> None:
        """Stops the polling loop."""
        self._running = False
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()


telegram_bot = TelegramBot()
