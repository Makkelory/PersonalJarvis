"""pc_control and network_status: everyday PC actions a classic user performs.

Fork addition (2026-11-14). ``pc_control`` covers the active window (minimize,
maximize, restore, close), show-desktop, media keys, lock, sleep, shutdown and
restart (with a cancelable delay), screenshots, the clipboard, Settings pages
and the date/time. ``network_status`` reports adapters and a real ping.

Per-action risk tiers: harmless actions are ``monitor``; power actions are
``ask`` so they follow whatever approval policy the owner configured. Shutdown
and restart always carry a delay so a misheard command can be cancelled with
``shutdown_cancel``.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
_SETTINGS = {
    "wifi": "ms-settings:network-wifi", "network": "ms-settings:network", "bluetooth": "ms-settings:bluetooth",
    "sound": "ms-settings:sound", "display": "ms-settings:display", "apps": "ms-settings:appsfeatures",
    "update": "ms-settings:windowsupdate", "power": "ms-settings:powersleep", "default_apps": "ms-settings:defaultapps",
    "storage": "ms-settings:storagesense", "privacy": "ms-settings:privacy", "main": "ms-settings:",
}
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]
_NEVER_CLOSE_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_HELP_TEXT = "\n".join((
    'Я умею, голосом и в чате:',
    '- Программы: «открой блокнот», «закрой стим», «какие программы запущены», «просканируй диски».',
    '- Звук: «громкость 50», «громче», «выключи звук», «пауза», «следующий трек».',
    '- Компьютер: «состояние компьютера», «проверь интернет», «сделай скриншот», «который час».',
    '- Окна и питание: «сверни все окна», «закрой окно», «заблокируй компьютер», «выключи компьютер» (30 секунд на отмену).',
    '- Файлы: «найди файл договор», «открой загрузки», «открой последний скачанный файл», «запиши заметку ...».',
    '- Время и справки: «таймер на 5 минут», «напомни через 10 минут ...», «погода», «курс доллара», «посчитай 15 процентов от 3400».',
    '- Интернет: «найди в интернете ...».',
))
_ASK_ACTIONS = {"sleep", "shutdown", "restart", "close_window", "clipboard_set"}


def _tap(*vks: int) -> None:
    """Press the given virtual keys together (modifiers first) and release."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, 2, 0)


def _foreground() -> tuple[int, str, str]:
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return int(hwnd or 0), buf.value, cls.value


def _ps(script: str, timeout: int = 20) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(  # noqa: S603 — fixed argv, script built in this module
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout, check=False, creationflags=_NOWIN,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace").strip()[:300] or "powershell failed")
    return (proc.stdout or b"").decode("utf-8", "replace").strip()


def _screenshot() -> str:
    import mss
    import mss.tools

    out_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "Screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"jarvis_{datetime.now():%Y%m%d_%H%M%S}.png"
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])  # every monitor
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    return str(path)


def _do(action: str, args: dict[str, Any]) -> str:  # noqa: C901 — flat dispatch
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    if action in _MEDIA_KEYS:
        _tap(_MEDIA_KEYS[action])
        label = {"play_pause": "пауза/воспроизведение", "next": "следующий трек",
                 "previous": "предыдущий трек", "stop": "стоп"}[action]
        return f"Отправил медиаклавишу: {label}."
    if action == "show_desktop":
        _tap(0x5B, 0x44)  # Win+D
        return "Показал рабочий стол (Win+D)."
    if action == "minimize_all":
        _tap(0x5B, 0x4D)  # Win+M
        return "Свернул все окна."
    if action in {"minimize_window", "maximize_window", "restore_window", "close_window"}:
        hwnd, title, cls = _foreground()
        if not hwnd or cls in _NEVER_CLOSE_CLASSES:
            raise ValueError("нет активного окна программы (активен рабочий стол или панель задач)")
        if action == "close_window":
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: same as the X button
            return f"Закрыл активное окно «{title[:60]}»."
        cmd = {"minimize_window": 6, "maximize_window": 3, "restore_window": 9}[action]
        user32.ShowWindow(hwnd, cmd)
        verb = {"minimize_window": "Свернул", "maximize_window": "Развернул", "restore_window": "Восстановил"}[action]
        return f"{verb} окно «{title[:60]}»."
    if action == "lock":
        user32.LockWorkStation()
        return "Компьютер заблокирован."
    if action == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=_NOWIN)  # noqa: S603
        return "Перевожу компьютер в режим сна."
    if action in {"shutdown", "restart"}:
        delay = max(5, min(600, int(args.get("delay_s") or 30)))
        flag = "/s" if action == "shutdown" else "/r"
        subprocess.run(["shutdown", flag, "/t", str(delay)], check=True, creationflags=_NOWIN)  # noqa: S603
        what = "Выключение" if action == "shutdown" else "Перезагрузка"
        return f"{what} через {delay} с. Чтобы отменить, скажи «отмени выключение»."
    if action == "shutdown_cancel":
        res = subprocess.run(["shutdown", "/a"], capture_output=True, check=False, creationflags=_NOWIN)  # noqa: S603
        if res.returncode == 0:
            return "Выключение и перезагрузка отменены."
        return "Нечего отменять: выключение не было запланировано."
    if action == "screenshot":
        return f"Скриншот сохранён: {_screenshot()}"
    if action == "clipboard_get":
        text = _ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $t=Get-Clipboard -Raw; if($t){$t}")
        return text[:2000] if text else "Буфер обмена пуст или в нём не текст."
    if action == "clipboard_set":
        payload = base64.b64encode(str(args.get("text") or "").encode("utf-8")).decode("ascii")
        _ps(f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')))")
        return "Текст скопирован в буфер обмена."
    if action == "open_settings":
        page = str(args.get("page") or "main").lower()
        uri = _SETTINGS.get(page)
        if uri is None:
            raise ValueError(f"неизвестная страница настроек; доступны: {', '.join(sorted(_SETTINGS))}")
        os.startfile(uri)  # type: ignore[attr-defined]  # noqa: S606 — fixed ms-settings URI
        return f"Открыл настройки Windows: {page}."
    if action == "help":
        return _HELP_TEXT
    if action == "time":
        now = datetime.now()
        return (f"Сейчас {now:%H:%M}, {_WEEKDAYS[now.weekday()]}, "
                f"{now.day} {_MONTHS[now.month - 1]} {now.year} года.")
    raise ValueError(f"unknown action {action!r}")


class PcControlTool:
    name: str = "pc_control"
    risk_tier: str = "monitor"
    description: str = (
        "Everyday PC actions. action: minimize_window, maximize_window, "
        "restore_window, close_window (active window), show_desktop, "
        "minimize_all, play_pause, next, previous, stop (media keys), lock, "
        "sleep, shutdown / restart (optional delay_s, default 30), "
        "shutdown_cancel, screenshot, clipboard_get, clipboard_set (text), "
        "open_settings (page: wifi, network, bluetooth, sound, display, apps, "
        "update, power, default_apps, storage, privacy, main), time. help (list what the assistant can do)."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "minimize_window", "maximize_window", "restore_window", "close_window", "show_desktop",
                "minimize_all", "play_pause", "next", "previous", "stop", "lock", "sleep", "shutdown",
                "restart", "shutdown_cancel", "screenshot", "clipboard_get", "clipboard_set",
                "open_settings", "time", "help"]},
            "text": {"type": "string", "description": "Text for clipboard_set"},
            "page": {"type": "string", "description": "Settings page for open_settings"},
            "delay_s": {"type": "integer", "description": "Delay before shutdown/restart, 5-600 s", "default": 30},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:
        return "ask" if str(args.get("action") or "").lower() in _ASK_ACTIONS else "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="pc_control supports Windows only")
        action = str(args.get("action") or "").lower()
        logger.info("[PC_CONTROL] action=%s trace=%s", action, ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_do, action, args))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PC_CONTROL] %s failed", action, exc_info=True)
            return ToolResult(success=False, output=None, error=f"{type(exc).__name__}: {exc}")


def _network_report() -> str:
    import psutil

    stats = psutil.net_if_stats()
    lines: list[str] = []
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if not st or not st.isup:
            continue
        v4 = [a.address for a in addrs if a.family == 2 and not a.address.startswith("169.254")]
        if v4 and not name.lower().startswith("loopback"):
            speed = f", {st.speed} Мбит/с" if st.speed else ""
            lines.append(f"- {name}: {', '.join(v4)}{speed}")
    out = ["Активные сетевые адаптеры:"] + (lines or ["- нет активных подключений"])
    for host in ("ya.ru", "8.8.8.8"):
        res = subprocess.run(  # noqa: S603
            ["ping", "-n", "3", "-w", "1500", host], capture_output=True, check=False, creationflags=_NOWIN,
        )
        text = res.stdout.decode("cp866", "replace")
        loss = re.search(r"\((\d+)%", text)
        avg = re.search(r"(?:Среднее|Average)\s*=\s*(\d+)", text)
        if loss and int(loss.group(1)) < 100:
            out.append(f"Пинг {host}: потери {loss.group(1)}%" + (f", среднее {avg.group(1)} мс." if avg else "."))
        else:
            out.append(f"Пинг {host}: нет ответа.")
    return "\n".join(out)


class NetworkStatusTool:
    name: str = "network_status"
    risk_tier: str = "safe"
    description: str = (
        "Checks the internet connection: active network adapters with IP "
        "addresses and a real ping to ya.ru and 8.8.8.8. Call it before "
        "answering whether the internet works."
    )
    schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="network_status supports Windows only")
        logger.info("[NETWORK_STATUS] trace=%s", ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_network_report))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[NETWORK_STATUS] failed", exc_info=True)
            return ToolResult(success=False, output=None, error=f"{type(exc).__name__}: {exc}")


