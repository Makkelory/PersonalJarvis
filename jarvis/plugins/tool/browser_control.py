"""browser_control: sites, searches and tabs in the user's browser.

Fork addition (2026-11-14). Opening a site or a search goes through the default
browser (``os.startfile(url)``), so it works in Chrome, Yandex, Edge, Firefox
alike. Tab actions focus the browser window that is open and send its standard
shortcuts (Ctrl+T / Ctrl+W / Ctrl+Tab ...). It cannot read page text; it says so.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.parse
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

SITES: dict[str, str] = {
    "ютуб": "https://www.youtube.com", "youtube": "https://www.youtube.com",
    "гугл": "https://www.google.com", "google": "https://www.google.com",
    "яндекс": "https://ya.ru", "яндекс почта": "https://mail.yandex.ru", "почта": "https://mail.yandex.ru",
    "гмайл": "https://mail.google.com", "джимейл": "https://mail.google.com", "gmail": "https://mail.google.com",
    "гитхаб": "https://github.com", "github": "https://github.com", "вк": "https://vk.com",
    "вконтакте": "https://vk.com", "твич": "https://www.twitch.tv", "twitch": "https://www.twitch.tv",
    "википедия": "https://ru.wikipedia.org", "карты": "https://yandex.ru/maps", "яндекс карты": "https://yandex.ru/maps",
    "переводчик": "https://translate.yandex.ru", "авито": "https://www.avito.ru", "озон": "https://www.ozon.ru",
    "вайлдберриз": "https://www.wildberries.ru", "хабр": "https://habr.com", "кинопоиск": "https://www.kinopoisk.ru",
    "стим магазин": "https://store.steampowered.com", "чат гпт": "https://chatgpt.com", "клод": "https://claude.ai",
    "телеграм веб": "https://web.telegram.org", "дискорд веб": "https://discord.com/app",
    "ютуб музыка": "https://music.youtube.com", "яндекс музыка": "https://music.yandex.ru",
}
_ENGINES = {"yandex": "https://yandex.ru/search/?text=", "google": "https://www.google.com/search?q=",
            "youtube": "https://www.youtube.com/results?search_query="}
_BROWSER_PROCS = ("chrome", "msedge", "browser", "firefox", "opera", "brave", "vivaldi")
_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "t": 0x54, "w": 0x57, "n": 0x4E, "j": 0x4A, "h": 0x48,
       "tab": 0x09, "left": 0x25, "right": 0x27, "f5": 0x74, "pgup": 0x21, "pgdn": 0x22}
_KEYS: dict[str, tuple[str, ...]] = {
    "new_tab": ("ctrl", "t"), "close_tab": ("ctrl", "w"), "reopen_tab": ("ctrl", "shift", "t"),
    "next_tab": ("ctrl", "pgdn"), "prev_tab": ("ctrl", "pgup"), "reload": ("f5",),
    "back": ("alt", "left"), "forward": ("alt", "right"), "new_window": ("ctrl", "n"),
    "incognito": ("ctrl", "shift", "n"), "downloads": ("ctrl", "j"), "history": ("ctrl", "h"),
}
_ACTIONS = ["open_url", "search", *_KEYS, "focus"]
_SAID = {
    "new_tab": "Открыл новую вкладку.", "close_tab": "Закрыл вкладку.", "reopen_tab": "Вернул закрытую вкладку.",
    "next_tab": "Переключил на следующую вкладку.", "prev_tab": "Переключил на предыдущую вкладку.",
    "reload": "Обновил страницу.", "back": "Вернулся назад.", "forward": "Перешёл вперёд.",
    "new_window": "Открыл новое окно.", "incognito": "Открыл окно инкогнито.",
    "downloads": "Открыл загрузки браузера.", "history": "Открыл историю браузера.",
}


def site_url(spoken: str) -> str | None:
    key = " ".join((spoken or "").lower().replace("ё", "е").split())
    for noise in ("сайт ", "сайта "):
        key = key.removeprefix(noise)
    if key in SITES:
        return SITES[key]
    if "." in key and " " not in key and not key.endswith("."):
        return key if key.startswith("http") else "https://" + key
    return None


def _browser_window():
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    for proc in _BROWSER_PROCS:
        win = ui.find_window(proc)
        if win is not None:
            return win
    return None


def _do(action: str, args: dict[str, Any]) -> str:
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    if action == "open_url":
        raw = str(args.get("url") or args.get("site") or "").strip()
        url = site_url(raw) or (raw if raw.startswith(("http://", "https://")) else None)
        if url is None:
            return f"Не знаю сайт «{raw}». Назови адрес, например youtube.com."
        os.startfile(url)  # noqa: S606 - opens in the default browser
        return f"Открыл {url}."
    if action == "search":
        query = str(args.get("query") or "").strip()
        if not query:
            return "Что искать?"
        engine = str(args.get("engine") or "yandex").lower()
        base = _ENGINES.get(engine, _ENGINES["yandex"])
        os.startfile(base + urllib.parse.quote(query))  # noqa: S606
        return f"Ищу «{query}» ({engine})."
    win = _browser_window()
    if win is None:
        if action in {"new_tab", "new_window"}:
            os.startfile("https://ya.ru")  # noqa: S606
            return "Браузер не был открыт, запустил его."
        return "Не вижу открытого браузера."
    if not ui.focus(win, settle=0.5):
        return "Не смог вывести браузер на передний план."
    if action == "focus":
        return "Браузер на переднем плане."
    ui.chord(*(_VK[k] for k in _KEYS[action]))
    time.sleep(0.2)
    return _SAID[action]


class BrowserControlTool:
    name: str = "browser_control"
    risk_tier: str = "monitor"
    description: str = (
        "Browser: open_url (url or a spoken site name like 'ютуб'), search (query, engine yandex|google|youtube), "
        "new_tab, close_tab, reopen_tab, next_tab, prev_tab, reload, back, forward, new_window, incognito, "
        "downloads, history, focus. Cannot read page contents."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS},
            "url": {"type": "string"}, "query": {"type": "string"},
            "engine": {"type": "string", "enum": list(_ENGINES)},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:  # noqa: ARG002
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="browser_control works on Windows only")
        action = str(args.get("action") or "")
        if action not in _ACTIONS:
            return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
        try:
            text = await asyncio.to_thread(_do, action, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("browser_control %s failed", action, exc_info=True)
            return ToolResult(success=False, output=None, error=f"browser_control {action}: {exc}")
        return ToolResult(success=True, output=text)
