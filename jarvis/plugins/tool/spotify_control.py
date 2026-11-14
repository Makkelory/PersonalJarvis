"""spotify_control: drive the Spotify DESKTOP app locally (no account link, no Premium API).

Fork addition (2026-11-14). The marketplace ``spotify`` tool needs an OAuth
connection and Premium; this one only needs the desktop app to be running.

* next / previous / play-pause use the global Windows media keys and report the
  new track from the window title (Spotify puts "Artist - Track" there).
* shuffle / repeat / like press Spotify's own shortcuts (Ctrl+S, Ctrl+R,
  Alt+Shift+B) with the window focused, then READ the result back from the
  player bar (the icons turn green) and report the real state. If the state did
  not change, the tool says so instead of claiming success.
* "radio" right-clicks the playing track, finds the "radio" entry of the context
  menu with OCR and clicks it, then presses the green play button.

The previous foreground window is restored afterwards. Windows only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import os
import time
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform
from jarvis.plugins.tool import ui_helpers as ui

logger = logging.getLogger(__name__)

_IDLE_TITLES = {"spotify", "spotify free", "spotify premium", ""}
_MEDIA = {"next": 0xB0, "previous": 0xB1, "play_pause": 0xB3}
_ACTIONS = ["now_playing", "next", "previous", "play", "pause", "play_pause", "random_track", "shuffle_on", "shuffle_off",
            "repeat_on", "repeat_off", "like", "unlike", "radio"]


def _track(title: str) -> str:
    return "" if title.strip().lower() in _IDLE_TITLES else title.strip()


def _wait_title_change(before: str, timeout: float = 3.5) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.4)
        win = ui.find_window("Spotify")
        if win and win.title != before:
            return win.title
    win = ui.find_window("Spotify")
    return win.title if win else before


def _bar_geometry(img, width: int, height: int) -> tuple[int, int]:
    """(play-button centre y, centre x) of the player bar: the white play circle."""
    cx = width // 2
    best_y, best = height - 74, -1
    for y in range(height - 130, height - 24, 2):
        crop = img.crop((cx - 13, y - 13, cx + 13, y + 13)).convert("L")
        white = sum(1 for p in crop.getdata() if p > 235)
        if white > best:
            best, best_y = white, y
    return best_y, cx


def _state(img, width: int, height: int) -> dict[str, bool]:
    y, cx = _bar_geometry(img, width, height)
    return {
        "shuffle": ui.green_pixels(img, (cx - 97, y - 14, cx - 65, y + 14)) > 12,
        "repeat": ui.green_pixels(img, (cx + 64, y - 14, cx + 96, y + 14)) > 12,
        "liked": ui.green_pixels(img, (60, y - 22, max(120, cx - 140), y + 22)) > 40,
    }


def _grab(win: ui.Win):
    left, top, width, height = ui.window_rect(win.handle)
    return ui.capture(left, top, width, height), left, top, width, height


def _toggle_until(win: ui.Win, key: str, vks: tuple[int, ...], want: bool, retries: int = 2) -> bool:
    """Press ``vks`` until the bar shows ``key`` == want. Returns the final state."""
    for _ in range(retries + 1):
        img, _l, _t, w, h = _grab(win)
        current = _state(img, w, h)[key]
        if current == want:
            return current
        ui.chord(*vks)
        time.sleep(0.9)
    img, _l, _t, w, h = _grab(win)
    return _state(img, w, h)[key]


def _radio(win: ui.Win) -> str:
    img, left, top, width, height = _grab(win)
    y, cx = _bar_geometry(img, width, height)
    click_x, click_y = 110, y + 6  # the track title in the player bar (window coordinates)
    ui.right_click(left + click_x, top + click_y)
    time.sleep(0.9)
    try:
        menu, left, top, width, height = _grab(win)

        def near_menu(words, pattern):
            # the context menu opens right above the click; the page header also
            # says "радио", so only words close to the click point count
            found = [w for w in words if re.search(pattern, w.text, re.I)
                     and abs(w.cx - click_x) < 420 and 0 < click_y - w.cy < 520]
            return min(found, key=lambda w: abs(w.cy - click_y)) if found else None

        hit = near_menu(ui.ocr(menu, lang="ru"), r"радио")
        if hit is None:
            hit = near_menu(ui.ocr(menu, lang="en-US"), r"radio")
    except Exception:
        ui.chord(ui.VK["esc"])
        raise
    if hit is None:
        ui.chord(ui.VK["esc"])
        raise RuntimeError("в меню трека не нашёл пункт «радио» (возможно, меню другое)")
    ui.click(left + hit.cx, top + hit.cy)
    time.sleep(2.0)
    page, left, top, width, height = _grab(win)
    # The hero title is large white text on a cover image, which the OCR often
    # reads as "адио" / "е:радио": match the tail of the word, not the whole of it.
    header = " ".join(w.text for w in ui.ocr(page.crop((80, 300, 700, 420)), lang="ru")).lower()
    spot = _find_play_button(page)
    # OCR over a cover image is unreliable, so a big green play button in the hero
    # position (a playlist/radio page header) also counts as "the page opened".
    opened = "адио" in header or (spot is not None and spot[1] > 380)
    started = False
    if opened and spot is not None:
        ui.click(left + spot[0], top + spot[1])
        time.sleep(1.5)
        started = True
    now = ui.find_window("Spotify")
    playing = f" Играет: {_track(now.title)}." if now and _track(now.title) else ""
    if not opened:
        return "Нажал «радио», но страницу радио по заголовку не подтвердил." + playing
    if not started:
        return "Открыл страницу радио, но зелёную кнопку воспроизведения не нашёл." + playing
    return "Открыл радио по треку и запустил воспроизведение." + playing


def _find_play_button(page) -> tuple[int, int] | None:
    """Centre of the big green round play button on a playlist/radio page, or None.

    A 40x40 window with the most Spotify-green pixels inside the left part of the
    content panel; the playing-row equaliser and check marks are far too small to win.
    """
    x0, y0, x1, y1 = 85, 55, 240, 660
    cell = 10
    cols, rows = (x1 - x0) // cell, (y1 - y0) // cell
    grid = [[0] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            grid[r][c] = ui.green_pixels(page, (x0 + c * cell, y0 + r * cell, x0 + (c + 1) * cell, y0 + (r + 1) * cell))
    best, best_rc = 0, None
    for r in range(rows - 3):
        for c in range(cols - 3):
            total = sum(grid[r + i][c + j] for i in range(4) for j in range(4))
            if total > best:
                best, best_rc = total, (r, c)
    if best_rc is None or best < 900:
        return None
    r, c = best_rc
    return x0 + c * cell + 20, y0 + r * cell + 20


def _play_or_pause(action: str, win: "ui.Win | None") -> str:
    """Make Spotify play / pause, reading its state first (the title shows the track while playing)."""
    if win is None:
        if action == "pause":
            return "Spotify не запущен, ставить на паузу нечего."
        os.startfile("spotify:")  # noqa: S606 - the registered Spotify URI scheme
        deadline = time.monotonic() + 12
        while win is None and time.monotonic() < deadline:
            time.sleep(0.6)
            win = ui.find_window("Spotify")
        if win is None:
            return "Запустил Spotify, но его окно так и не появилось."
        time.sleep(2.5)
        win = ui.find_window("Spotify") or win
        if _track(win.title):
            return f"Запустил Spotify, играет: {_track(win.title)}."
    playing = bool(_track(win.title))
    if action == "play" and playing:
        return f"Уже играет: {_track(win.title)}."
    if action == "pause" and not playing:
        return "Музыка и так на паузе."
    before = win.title
    ui.chord(_MEDIA["play_pause"])
    after = _wait_title_change(before)
    now_playing = bool(_track(after))
    if action == "play":
        return f"Включил: {_track(after)}." if now_playing else "Нажал «играть», но Spotify остался на паузе: в нём нет трека для запуска."
    return "Поставил на паузу." if not now_playing else f"Нажал паузу, но музыка ещё играет: {_track(after)}."


def _do(action: str) -> str:  # noqa: C901 — flat dispatch
    win = ui.find_window("Spotify")
    if action == "now_playing":
        if win is None:
            return "Окна Spotify нет (приложение закрыто или свёрнуто в трей)."
        track = _track(win.title)
        return f"Сейчас играет: {track}." if track else "Сейчас ничего не играет (Spotify на паузе)."
    if action in {"play", "pause"}:
        return _play_or_pause(action, win)
    if action in _MEDIA:
        before = win.title if win else ""
        ui.chord(_MEDIA[action])
        if win is None:
            return "Отправил медиаклавишу, но окна Spotify не вижу: проверить результат не могу."
        after = _wait_title_change(before)
        if action == "play_pause":
            track = _track(after)
            return f"Играет: {track}." if track else "Поставил на паузу."
        if after == before:
            return f"Нажал «{ 'следующий' if action == 'next' else 'предыдущий'}», но название не изменилось: {_track(after) or 'нет трека'}."
        return f"Переключил. Теперь играет: {_track(after)}."
    if win is None:
        raise RuntimeError("Spotify не запущен или свёрнут в трей: открой его, тогда смогу переключать режимы")
    previous = ui.foreground()
    try:
        if not ui.focus(win):
            raise RuntimeError("не удалось вывести Spotify на передний план")
        if action in {"shuffle_on", "shuffle_off", "random_track"}:
            want = action != "shuffle_off"
            state = _toggle_until(win, "shuffle", (ui.VK["ctrl"], ui.VK["s"]), want)
            if state != want:
                raise RuntimeError("перемешивание не переключилось (сочетание Ctrl+S не сработало)")
            text = "Перемешивание включено." if state else "Перемешивание выключено."
            if action == "random_track":
                before = ui.find_window("Spotify").title
                ui.chord(_MEDIA["next"])
                after = _wait_title_change(before)
                text += f" Играет: {_track(after)}."
            return text
        if action in {"repeat_on", "repeat_off"}:
            want = action == "repeat_on"
            state = _toggle_until(win, "repeat", (ui.VK["ctrl"], ui.VK["r"]), want, retries=3)
            if state != want:
                raise RuntimeError("повтор не переключился (сочетание Ctrl+R не сработало)")
            return "Повтор включён." if state else "Повтор выключен."
        if action in {"like", "unlike"}:
            want = action == "like"
            img, _l, _t, w, h = _grab(win)
            if _state(img, w, h)["liked"] == want:
                return "Трек уже в любимом." if want else "Трека и так нет в любимом."
            ui.chord(ui.VK["alt"], ui.VK["shift"], ui.VK["b"])
            time.sleep(1.2)
            img, _l, _t, w, h = _grab(win)
            if _state(img, w, h)["liked"] != want:
                raise RuntimeError("метка «любимое» не изменилась (Alt+Shift+B не сработало)")
            return "Добавил трек в любимое." if want else "Убрал трек из любимого."
        if action == "radio":
            return _radio(win)
        raise ValueError(f"unknown action {action!r}")
    finally:
        ui.restore_focus(previous)


class SpotifyControlTool:
    name: str = "spotify_control"
    risk_tier: str = "monitor"
    description: str = (
        "Controls the Spotify desktop app on this PC. action: now_playing, next, "
        "previous, play_pause, random_track (shuffle on + next), shuffle_on, "
        "shuffle_off, repeat_on, repeat_off, like (add track to Liked Songs), "
        "unlike, radio (start the song radio of the playing track). It reads the "
        "result back from the app and reports the real state."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": _ACTIONS}},
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="spotify_control supports Windows only")
        action = str(args.get("action") or "now_playing").lower()
        if action not in _ACTIONS:
            return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
        logger.info("[SPOTIFY] action=%s trace=%s", action, ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_do, action))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SPOTIFY] %s failed: %s", action, exc)
            return ToolResult(success=False, output=None, error=f"Spotify: {exc}")
