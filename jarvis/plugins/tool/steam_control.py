"""steam_control: libraries, install to a chosen drive, run games, downloads, friend status.

Fork addition (2026-11-14). Steam exposes ``steam://`` URLs for most things and a
plain-text library layout; the one thing it does not expose is the choice of the
install drive, which only exists in its own "Установить" dialog. That dialog is a
separate window, so the tool drives it like a person would:

1. ``steam://install/<appid>`` opens the dialog;
2. the drive row ("Локальный диск (C:)", "SSD 500GB (E:)", ...) is found by OCR and
   clicked; the highlighted row is checked by colour before going on;
3. the dialog is scrolled to its "Установить" button and the button is pressed;
4. success is confirmed by the new ``appmanifest_<appid>.acf`` appearing in the
   chosen library (Steam's standard ``steamapps`` folder on that drive).

Nothing is ever purchased: if the dialog is not the install dialog (a game that is
not in the library and is not free), the tool stops and says so. Windows only.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform
from jarvis.plugins.tool import ui_helpers as ui

logger = logging.getLogger(__name__)

_ACTIONS = ["libraries", "find", "run", "install", "open_downloads", "pause_downloads",
            "resume_downloads", "friend_status"]
_STATES = {
    "online": "online", "away": "away", "invisible": "invisible", "offline": "offline",
    "busy": "busy", "snooze": "snooze",
    "в сети": "online", "онлайн": "online", "нет на месте": "away", "отошёл": "away", "отошел": "away",
    "невидимка": "invisible", "невидимым": "invisible", "не в сети": "offline", "оффлайн": "offline",
    "офлайн": "offline", "не беспокоить": "busy", "спит": "snooze",
}
_STATE_RU = {"online": "в сети", "away": "нет на месте", "invisible": "невидимка", "offline": "не в сети",
             "busy": "не беспокоить", "snooze": "спит"}


# ----------------------------------------------------------------------
# Steam files
# ----------------------------------------------------------------------
def steam_root() -> Path | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(str(winreg.QueryValueEx(key, "SteamPath")[0]).replace("/", "\\"))
    except OSError:
        return None


def libraries() -> list[Path]:
    """Steam library roots (the folder that contains ``steamapps``)."""
    root = steam_root()
    if root is None:
        return []
    found = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for raw in re.findall(r'"path"\s+"([^"]+)"', text):
        path = Path(raw.replace("\\\\", "\\"))
        if path not in found:
            found.append(path)
    return [p for p in found if (p / "steamapps").is_dir()]


def installed_games() -> list[tuple[int, str, Path]]:
    """(appid, name, library) for every appmanifest in every library."""
    games: list[tuple[int, str, Path]] = []
    for lib in libraries():
        for manifest in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]*)"', text)
            if appid and name:
                games.append((int(appid.group(1)), name.group(1), lib))
    return games


def _find_local(query: str) -> tuple[int, str, Path] | None:
    q = query.casefold().strip()
    games = installed_games()
    exact = [g for g in games if g[1].casefold() == q]
    if exact:
        return exact[0]
    partial = sorted((g for g in games if q in g[1].casefold()), key=lambda g: len(g[1]))
    return partial[0] if partial else None


def _store_search(query: str) -> tuple[int, str] | None:
    url = ("https://store.steampowered.com/api/storesearch/?" +
           urllib.parse.urlencode({"term": query, "l": "russian", "cc": "RU"}))
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — fixed https Steam store endpoint
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("items") or []
    if not items:
        return None
    q = query.casefold()
    best = next((i for i in items if str(i.get("name", "")).casefold() == q), items[0])
    return int(best["id"]), str(best["name"])


def resolve(query: str) -> tuple[int, str, Path | None, bool]:
    """(appid, name, library-if-installed, from_store). A digit string is an appid."""
    query = query.strip()
    if query.isdigit():
        local = next((g for g in installed_games() if g[0] == int(query)), None)
        return (int(query), local[1] if local else f"app {query}", local[2] if local else None, False)
    local = _find_local(query)
    if local:
        return local[0], local[1], local[2], False
    found = _store_search(query)
    if found is None:
        raise ValueError(f"игру «{query}» не нашёл ни среди установленных, ни в магазине Steam")
    return found[0], found[1], None, True


def _free_gb(path: Path) -> float:
    import shutil

    try:
        return shutil.disk_usage(path).free / 2**30
    except OSError:
        return 0.0


# ----------------------------------------------------------------------
# Install dialog
# ----------------------------------------------------------------------
# Steam shows "Установить" in one of two ways: as a separate small window (when the
# Steam main window is in the background) or as a modal overlay INSIDE the main
# window (when it is in front). Both look the same, so both are handled here.
_DIALOG_TITLES = ("Установить", "Install")


def _popup() -> ui.Win | None:
    for title in _DIALOG_TITLES:
        win = ui.find_window("steamwebhelper", title)
        if win is not None:
            return win
    return None


def _grab(win: ui.Win):
    left, top, width, height = ui.window_rect(win.handle)
    return ui.capture(left, top, width, height), left, top, width, height


def _drive_rows(img) -> dict[str, tuple[int, int]]:
    """{'C': (x, y), ...} for every "(C:)" label found in the dialog, in image pixels.

    Each OCR language misses some labels ("(E:)" and "(F:)" were invisible to the
    Russian engine but clean in the English one), so both are read and merged."""
    rows: dict[str, tuple[int, int]] = {}
    for lang in ("ru", "en-US"):
        for word in ui.ocr(img, lang=lang):
            m = re.search(r"\(([A-Za-zА-Яа-я]):?\)", word.text)
            if m:
                letter = m.group(1).upper().translate(str.maketrans("СВАЕНКМОРТХ", "CBAEHKMOPTX"))
                rows.setdefault(letter, (word.cx, word.cy))
    return rows


def _wait_dialog(timeout: float = 25.0) -> tuple[ui.Win, dict[str, tuple[int, int]]]:
    """The window that hosts the install dialog and its drive rows."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        popup = _popup()
        candidates = [popup] if popup is not None else []
        main = ui.find_window("steamwebhelper", "Steam")
        if main is not None:
            candidates.append(main)
        for win in candidates:
            if not ui.focus(win, settle=0.9):
                continue
            img, *_ = _grab(win)
            rows = _drive_rows(img)
            if rows:
                return win, rows
        time.sleep(0.8)
    raise RuntimeError("окно установки Steam не появилось (игра платная и не в библиотеке, либо Steam не запущен)")


def _row_selected(img, x: int, y: int) -> bool:
    """The chosen drive row is painted bright blue (#1A9FFF) around its label."""
    hits = total = 0
    for dx in range(20, 220, 10):
        px, py = min(max(x + dx, 0), img.width - 1), min(max(y, 0), img.height - 1)
        r, g, b = img.getpixel((px, py))
        total += 1
        if r < 80 and 110 < g < 210 and b > 215:
            hits += 1
    return hits >= max(6, total // 3)


def _scroll_dialog(left: int, top: int, notches: int = 6) -> None:
    ctypes.windll.user32.SetCursorPos(left + 270, top + 300)  # type: ignore[attr-defined]
    for _ in range(notches):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120, 0)  # type: ignore[attr-defined]
        time.sleep(0.15)
    time.sleep(0.8)


def _button(win: ui.Win, names: tuple[str, ...]) -> tuple[int, int] | None:
    """Screen point of the lowest on-screen word that starts with one of ``names``."""
    img, left, top, _w, _h = _grab(win)
    hits = [w for w in ui.ocr(img, lang="ru") if w.text.lower().startswith(names)]
    if not hits:
        return None
    lowest = max(hits, key=lambda w: w.cy)  # the dialog title says "Установить" too: buttons are lower
    return left + lowest.cx, top + lowest.cy


def _install(query: str, drive: str, dry_run: bool) -> str:
    appid, name, library, from_store = resolve(query)
    if library is not None:
        return f"«{name}» уже установлена: {library}."
    letter = drive.strip().rstrip(":\\/ ").upper()
    previous = ui.foreground()
    os.startfile(f"steam://install/{appid}")  # type: ignore[attr-defined]  # noqa: S606 — Steam's own URL handler
    try:
        win, rows = _wait_dialog()
        img, left, top, _w, _h = _grab(win)
        chosen = ""
        if letter:
            if letter not in rows:
                raise RuntimeError(f"диска {letter}: нет в окне установки; доступны: {', '.join(sorted(rows))}")
            x, y = rows[letter]
            for _attempt in range(2):
                ui.click(left + x, top + y)
                time.sleep(0.8)
                check, left, top, _w, _h = _grab(win)
                if _row_selected(check, x, y):
                    break
            else:
                raise RuntimeError(f"не удалось выбрать диск {letter}: в окне Steam")
            chosen = letter
        install_btn = _button(win, ("установить", "install"))
        # a popup is small: the buttons are below the fold until it is scrolled
        if install_btn is None or install_btn[1] - top < 330:
            _scroll_dialog(left, top)
            install_btn = _button(win, ("установить", "install"))
        cancel_btn = _button(win, ("отмена", "cancel"))
        if install_btn is None:
            raise RuntimeError("не нашёл кнопку «Установить» в окне Steam")
        where = f"диск {chosen}:" if chosen else "диск по умолчанию (★ в окне Steam)"
        if dry_run:
            if cancel_btn:
                ui.click(*cancel_btn)
            return (f"Проверка без установки: окно «{name}» открыто, {where} выбран, кнопка «Установить» найдена. "
                    "Окно закрыто, ничего не скачивалось.")
        ui.click(*install_btn)
        # confirm through Steam's own bookkeeping: the manifest shows up in the library
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            time.sleep(1.5)
            for lib in libraries():
                if (lib / "steamapps" / f"appmanifest_{appid}.acf").exists():
                    drive_label = lib.drive or str(lib)
                    return (f"Установка «{name}» запущена, папка: {lib}\\steamapps\\common "
                            f"(свободно на {drive_label} {_free_gb(lib):.0f} ГБ).")
        raise RuntimeError(f"нажал «Установить» для «{name}», но Steam не создал запись об установке")
    finally:
        ui.restore_focus(previous)


# ----------------------------------------------------------------------
# Downloads page
# ----------------------------------------------------------------------
def _downloads_button(want_pause: bool) -> str:
    os.startfile("steam://open/downloads")  # type: ignore[attr-defined]  # noqa: S606
    time.sleep(3.0)
    win = ui.find_window("steamwebhelper", "Steam")
    if win is None:
        raise RuntimeError("окно Steam не найдено")
    previous = ui.foreground()
    try:
        ui.focus(win, settle=0.8)
        img, left, top, _w, _h = _grab(win)
        words = ui.ocr(img, lang="ru")
        wanted = ("приостанов", "пауза", "pause") if want_pause else ("возобнов", "продолж", "resume")
        hit = next((w for w in words if w.text.lower().startswith(wanted)), None)
        if hit is None:
            if "download" in " ".join(w.text.lower() for w in words) or "загрузк" in " ".join(w.text.lower() for w in words):
                return ("Открыл «Загрузки», но кнопки «" + ("Приостановить" if want_pause else "Продолжить") +
                        "» не вижу: возможно, сейчас нет активной загрузки или она уже в нужном состоянии.")
            raise RuntimeError("страницу загрузок на экране не распознал")
        ui.click(left + hit.cx, top + hit.cy)
        return ("Приостановил загрузки Steam." if want_pause else "Возобновил загрузки Steam.")
    finally:
        ui.restore_focus(previous)


# ----------------------------------------------------------------------
def _do(args: dict[str, Any]) -> str:  # noqa: C901 — flat dispatch
    action = str(args.get("action") or "libraries").lower()
    if action == "libraries":
        libs = libraries()
        if not libs:
            return "Steam не найден или у него нет библиотек."
        games = installed_games()
        lines = [f"Библиотеки Steam ({len(libs)}):"]
        for lib in libs:
            count = sum(1 for g in games if g[2] == lib)
            lines.append(f"- {lib} — игр: {count}, свободно {_free_gb(lib):.0f} ГБ")
        return "\n".join(lines)
    if action == "friend_status":
        raw = str(args.get("state") or "").strip().lower()
        state = _STATES.get(raw)
        if state is None:
            raise ValueError("состояние: в сети, нет на месте, невидимка, не в сети (online, away, invisible, offline)")
        os.startfile(f"steam://friends/status/{state}")  # type: ignore[attr-defined]  # noqa: S606
        return f"Отправил в Steam статус «{_STATE_RU[state]}»."
    if action == "open_downloads":
        os.startfile("steam://open/downloads")  # type: ignore[attr-defined]  # noqa: S606
        return "Открыл раздел «Загрузки» в Steam."
    if action in {"pause_downloads", "resume_downloads"}:
        return _downloads_button(action == "pause_downloads")
    query = str(args.get("game") or "").strip()
    if not query:
        raise ValueError("укажи игру (название или appid)")
    if action == "find":
        appid, name, library, _store = resolve(query)
        return (f"{name} (appid {appid}): установлена в {library}." if library
                else f"{name} (appid {appid}): не установлена.")
    if action == "run":
        appid, name, library, _store = resolve(query)
        if library is None:
            raise ValueError(f"«{name}» не установлена; скажи «установи {name}»")
        os.startfile(f"steam://rungameid/{appid}")  # type: ignore[attr-defined]  # noqa: S606
        return f"Запустил «{name}» через Steam."
    if action == "install":
        return _install(query, str(args.get("drive") or ""), bool(args.get("dry_run")))
    raise ValueError(f"unknown action {action!r}")


class SteamControlTool:
    name: str = "steam_control"
    risk_tier: str = "monitor"
    description: str = (
        "Controls Steam on this PC. action: libraries (drives with free space), "
        "find (game by name), run (game), install (game, drive letter: opens "
        "Steam's install dialog, picks that drive, presses Install; never buys "
        "anything), open_downloads, pause_downloads, resume_downloads, "
        "friend_status (state: в сети / нет на месте / невидимка / не в сети)."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS},
            "game": {"type": "string", "description": "Game name or appid"},
            "drive": {"type": "string", "description": "Install drive letter, e.g. E"},
            "state": {"type": "string", "description": "Friend status for friend_status"},
            "dry_run": {"type": "boolean", "description": "install: stop before pressing Install"},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:
        return "ask" if str(args.get("action") or "") == "install" and not args.get("dry_run") else "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="steam_control supports Windows only")
        logger.info("[STEAM] action=%s trace=%s", args.get("action"), ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_do, dict(args)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[STEAM] %s failed: %s", args.get("action"), exc)
            return ToolResult(success=False, output=None, error=f"Steam: {exc}")
