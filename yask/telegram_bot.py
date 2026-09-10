"""Telegram bot process for yask.

Run via ``yask telegram``: validates the bot token (``TELEGRAM_BOT_TOKEN``)
with ``getMe``, opens the yask store, then long-polls the Bot API with
``getUpdates`` and answers incoming messages. Today the command table is
``/start`` and ``/help``; later features of the Telegram interface (Epic #27)
extend the dispatch layer on top of the store passed in here.

The bot talks to the Bot API directly with ``httpx`` (already a project
dependency). The client accepts an injected ``httpx.AsyncClient`` so tests
can mock the Bot API at the HTTP layer with ``httpx.MockTransport`` — tests
never call real Telegram.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import db
from .store import Store

# Bot API base URL. Overridable for tests/future (no env override needed now).
DEFAULT_BASE_URL = "https://api.telegram.org"
# Long-poll timeout (seconds) passed to getUpdates.
POLL_TIMEOUT = 30
# httpx timeout (seconds) for the default client; must exceed the long-poll
# timeout so a poll blocking server-side is not cut off client-side.
HTTP_TIMEOUT = 65.0
# Cap on the 429 retry_after back-off (seconds).
MAX_RETRY_AFTER = 30.0

START_TEXT = (
    "Hello! I am the yask Telegram bot.\n\n"
    "I am connected to a yask kanban board and answer commands about it.\n\n"
    "Use /help to see what I can do."
)

HELP_TEXT = (
    "Commands:\n"
    "/start — introduction\n"
    "/help — this help\n\n"
    "I read the yask board that this process was started with\n"
    "(yask telegram --data DIR). More commands are on the way."
)

UNKNOWN_HINT = "I don't understand that. Try /help to see what I can do."

# Command table. Later bot features (project view, task list, notifications)
# extend this without changing the poll loop.
COMMANDS: dict[str, str] = {
    "/start": START_TEXT,
    "/help": HELP_TEXT,
}


class BotAPIError(Exception):
    """Bot API failure: an ``ok:false`` response or a transport failure.

    Carries the API ``error_code`` (when present) and a human-readable
    ``description``.
    """

    def __init__(self, description: str, error_code: Optional[int] = None) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.description


def reply_for(text: Optional[str]) -> Optional[str]:
    """Reply text for an incoming message, or None if there is nothing to say.

    Non-text messages (stickers, photos, ...) get no reply; unknown input
    gets a short "try /help" hint. A ``/command@botname`` suffix is ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    token = text.strip().split()[0]
    if not token.startswith("/"):
        return UNKNOWN_HINT
    cmd = token.split("@", 1)[0].lower()
    return COMMANDS.get(cmd, UNKNOWN_HINT)


def make_dispatch(store: Store) -> Callable[[Optional[str]], Optional[str]]:
    """Build the message→reply dispatcher for a bot bound to ``store``.

    The store is not consulted yet — the command table is static. Later bot
    features close over the store here to answer board queries.
    """
    return reply_for


class BotAPI:
    """Minimal Telegram Bot API client: getMe, getUpdates, sendMessage.

    Accepts an ``httpx.AsyncClient`` for tests (e.g. with
    ``httpx.MockTransport``); production uses a default client that
    ``aclose`` closes.
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._owns_client = client is None

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = await self._post(url, params)
            data = response.json()
        except httpx.HTTPError as exc:
            raise BotAPIError(f"telegram request failed: {exc}") from exc
        except ValueError as exc:
            raise BotAPIError(
                f"telegram returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if not data.get("ok", False):
            raise BotAPIError(
                data.get("description") or f"HTTP {response.status_code}",
                data.get("error_code"),
            )
        return data.get("result")

    async def _post(self, url: str, params: dict) -> httpx.Response:
        response = await self._client.post(url, json=params)
        if response.status_code == 429:
            # Rate limited: back off as requested (capped) and retry once.
            await asyncio.sleep(self._retry_after(response))
            response = await self._client.post(url, json=params)
        return response

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            seconds = float(response.json().get("parameters", {}).get("retry_after", 5))
        except (ValueError, TypeError, AttributeError):
            seconds = 5.0
        return min(max(seconds, 0.0), MAX_RETRY_AFTER)

    async def get_me(self) -> dict:
        result = await self._call("getMe")
        return result if isinstance(result, dict) else {}

    async def get_updates(
        self, offset: Optional[int] = None, timeout: int = POLL_TIMEOUT
    ) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        result = await self._call("getUpdates", **params)
        return list(result) if result else []

    async def send_message(self, chat_id: int, text: str) -> dict:
        result = await self._call("sendMessage", chat_id=chat_id, text=text)
        return result if isinstance(result, dict) else {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def run_bot(
    api: BotAPI,
    dispatch: Callable[[Optional[str]], Optional[str]],
    stop_event: Optional[asyncio.Event] = None,
    poll_timeout: int = POLL_TIMEOUT,
    error_delay: float = 1.0,
) -> None:
    """Long-poll ``getUpdates`` and dispatch message handlers until stopped.

    The offset advances to ``update_id + 1`` after each processed update.
    Transient :class:`BotAPIError` failures are logged and retried after
    ``error_delay``; they never stop the loop. Returns when ``stop_event`` is
    set.
    """
    offset: Optional[int] = None
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            updates = await api.get_updates(offset=offset, timeout=poll_timeout)
        except BotAPIError as exc:
            print(f"yask: telegram poll failed: {exc}", file=sys.stderr)
            if stop_event is not None and stop_event.is_set():
                return
            await asyncio.sleep(error_delay)
            continue
        for update in updates:
            update_id = update.get("update_id")
            message = update.get("message")
            try:
                if message is not None:
                    reply = dispatch(message.get("text"))
                    chat = message.get("chat") or {}
                    if reply is not None and "id" in chat:
                        await api.send_message(chat["id"], reply)
            except BotAPIError as exc:
                print(f"yask: telegram dispatch failed: {exc}", file=sys.stderr)
                await asyncio.sleep(error_delay)
            if update_id is not None:
                offset = update_id + 1


async def _amain(
    token: str,
    data_dir: Path,
    client: Optional[httpx.AsyncClient] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> int:
    api = BotAPI(token, client=client)
    # Fail fast on a bad token before touching disk.
    try:
        me = await api.get_me()
    except BotAPIError as exc:
        await api.aclose()
        print(f"yask: invalid Telegram bot token: {exc}", file=sys.stderr)
        return 1
    print(f"yask: telegram bot @{me.get('username')} (data: {data_dir})")

    conn = db.connect(data_dir / "yask.db")
    store = Store(conn)

    # Without an externally supplied stop event (production), SIGINT is the
    # stop trigger.
    loop = None
    handler_installed = False
    if stop_event is None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
            handler_installed = True
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # No signal-handler support here (non-POSIX, or not in the main
            # thread): main() falls back to catching KeyboardInterrupt.
            pass
    try:
        await run_bot(api, make_dispatch(store), stop_event=stop_event, poll_timeout=POLL_TIMEOUT)
    finally:
        if handler_installed:
            loop.remove_signal_handler(signal.SIGINT)
        await api.aclose()
        conn.close()
    return 0


def main(
    token: str,
    data_dir: str | Path,
    client: Optional[httpx.AsyncClient] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> int:
    """Sync entry point for the bot process; returns the exit code.

    Validates the token with getMe before touching disk, opens the store
    from ``data_dir``, then runs the poll loop until SIGINT. Exit 0 on a
    clean stop. ``client`` and ``stop_event`` are test seams (injected
    transport, injected stop signal); production passes neither.
    """
    try:
        return asyncio.run(_amain(token, Path(data_dir), client=client, stop_event=stop_event))
    except KeyboardInterrupt:
        print("yask: stopping telegram bot", file=sys.stderr)
        return 0
