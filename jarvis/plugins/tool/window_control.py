"""window_control: move, snap and resize windows, including across monitors.

Fork addition (2026-11-14). Pure ``ctypes`` + user32, no PowerShell. Monitors are
numbered the way Windows Settings numbers them (``\\\\.\\DISPLAY1`` = monitor 1).
A window is chosen by a spoken or written program/title fragment ("браузер",
"телеграм", "chrome"); with no fragment the active window is used (Jarvis' own
window is skipped).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
from ctypes import wintypes
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

SW_RESTORE, SW_MAXIMIZE, SW_MINIMIZE = 9, 3, 6
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
_OWN_PROCESSES = {"personaljarvis", "pythonw", "python", "jarvis"}
_ACTIONS = ["list_monitors", "list_windows", "move_to_monitor", "snap", "maximize", "minimize", "restore", "center"]
_SNAPS = {"left": (0.0, 0.0, 0.5, 1.0), "right": (0.5, 0.0, 0.5, 1.0), "top": (0.0, 0.0, 1.0, 0.5),
          "bottom": (0.0, 0.5, 1.0, 0.5), "full": (0.0, 0.0, 1.0, 1.0)}


class _Monitor:
    def __init__(self, number: int, handle: int, rect: tuple[int, int, int, int],
                 work: tuple[int, int, int, int], primary: bool) -> None:
        self.number, self.handle, self.rect, self.work, self.primary = number, handle, rect, work, primary

    def label(self) -> str:
        w, h = self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]
        return f"монитор {self.number}: {w}x{h}{' (основной)' if self.primary else ''}, слева {self.rect[0]}"


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def _user32():
    return ctypes.windll.user32  # type: ignore[attr-defined]


def _monitors() -> list[_Monitor]:
    user32 = _user32()
    found: list[_Monitor] = []
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(handle, _hdc, _rect, _lparam):  # noqa: ANN001
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            digits = re.search(r"(\d+)$", info.szDevice)
            number = int(digits.group(1)) if digits else len(found) + 1
            r, w = info.rcMonitor, info.rcWork
            found.append(_Monitor(number, int(handle or 0), (r.left, r.top, r.right, r.bottom),
                                  (w.left, w.top, w.right, w.bottom), bool(info.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, proc_t(_cb), 0)
    return sorted(found, key=lambda m: m.number)


def _process_stem(hwnd: int) -> str:
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0].lower()
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _windows() -> list[tuple[int, str, str]]:
    """Visible, titled, non-tool top-level windows in z-order: (hwnd, title, process)."""
    user32 = _user32()
    out: list[tuple[int, str, str]] = []
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title in {"Program Manager", "Settings"} and not _process_stem(hwnd):
            return True
        out.append((int(hwnd), title, _process_stem(hwnd)))
        return True

    user32.EnumWindows(proc_t(_cb), 0)
    return out


def _pick_window(query: str) -> tuple[int, str, str] | None:
    from jarvis.plugins.tool import app_catalog  # noqa: PLC0415

    wins = _windows()
    q = (query or "").strip().lower()
    if not q:
        fore = int(_user32().GetForegroundWindow() or 0)
        for item in wins:
            if item[0] == fore and item[2] not in _OWN_PROCESSES:
                return item
        return next((w for w in wins if w[2] not in _OWN_PROCESSES), None)
    wanted = {q, app_catalog.spoken_to_canonical(q).lower(), app_catalog._translit(q)}  # noqa: SLF001
    wanted = {w for w in wanted if w}
    for item in wins:
        stem, title = item[2], item[1].lower()
        if any(w == stem or w in stem or stem in w and len(stem) > 3 or w in title for w in wanted):
            return item
    return None


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    _user32().GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _monitor_of(hwnd: int, monitors: list[_Monitor]) -> _Monitor:
    handle = int(_user32().MonitorFromWindow(hwnd, 2) or 0)
    return next((m for m in monitors if m.handle == handle), monitors[0])


def _place(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    _user32().SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)


def _resolve_monitor(spec: Any, current: _Monitor, monitors: list[_Monitor]) -> _Monitor | None:
    text = str(spec).strip().lower()
    if text in {"next", "other", "другой", "соседний", "следующий", ""}:
        idx = monitors.index(current)
        return monitors[(idx + 1) % len(monitors)]
    if text in {"primary", "основной", "главный"}:
        return next((m for m in monitors if m.primary), monitors[0])
    words = {"первый": 1, "один": 1, "второй": 2, "два": 2, "третий": 3, "три": 3, "четвертый": 4, "четвёртый": 4, "четыре": 4}
    number = words.get(text)
    if number is None:
        digits = re.search(r"\d+", text)
        number = int(digits.group()) if digits else None
    if number is None:
        return None
    return next((m for m in monitors if m.number == number), None)


def _do(action: str, args: dict[str, Any]) -> str:  # noqa: C901 - flat dispatch
    user32 = _user32()
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: real pixels
    except Exception:  # noqa: BLE001
        pass
    monitors = _monitors()
    if action == "list_monitors":
        return "; ".join(m.label() for m in monitors) or "Мониторы не определились."
    if action == "list_windows":
        wins = [w for w in _windows() if w[2] not in _OWN_PROCESSES][:25]
        return "\n".join(f"{p or '?'}: {t[:70]}" for _h, t, p in wins) or "Окон нет."
    query = str(args.get("window") or args.get("app") or "")
    picked = _pick_window(query)
    if picked is None:
        return f"Не нашёл окно «{query}»." if query else "Нет подходящего активного окна."
    hwnd, title, proc = picked
    name = f"«{title[:50]}» ({proc})"
    if action == "minimize":
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return f"Свернул окно {name}."
    if action == "restore":
        user32.ShowWindow(hwnd, SW_RESTORE)
        return f"Восстановил окно {name}."
    current = _monitor_of(hwnd, monitors)
    if action == "maximize":
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return f"Развернул окно {name} на мониторе {current.number}."
    if action in {"snap", "center"}:
        wx, wy, wr, wb = current.work
        ww, wh = wr - wx, wb - wy
        user32.ShowWindow(hwnd, SW_RESTORE)
        if action == "center":
            l, t, r, b = _rect(hwnd)
            w, h = min(r - l, ww), min(b - t, wh)
            _place(hwnd, wx + (ww - w) // 2, wy + (wh - h) // 2, w, h)
            return f"Поставил окно {name} по центру монитора {current.number}."
        side = str(args.get("side") or "left").lower()
        side = {"лево": "left", "влево": "left", "лев": "left", "право": "right", "вправо": "right", "верх": "top",
                "вверх": "top", "низ": "bottom", "вниз": "bottom"}.get(side, side)
        fx, fy, fw, fh = _SNAPS.get(side, _SNAPS["left"])
        if side == "full":
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        else:
            _place(hwnd, wx + int(ww * fx), wy + int(wh * fy), int(ww * fw), int(wh * fh))
        return f"Прижал окно {name} ({side}) на мониторе {current.number}."
    if action == "move_to_monitor":
        if len(monitors) < 2:
            return "Монитор всего один, переносить некуда."
        target = _resolve_monitor(args.get("monitor", "next"), current, monitors)
        if target is None:
            return f"Нет такого монитора. Есть: {'; '.join(m.label() for m in monitors)}."
        if target.number == current.number:
            return f"Окно {name} уже на мониторе {target.number}."
        was_max = bool(user32.IsZoomed(hwnd))
        if was_max or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        l, t, r, b = _rect(hwnd)
        sx, sy, sr, sb = current.work
        tx, ty, tr, tb = target.work
        sw_, sh_, tw, th = sr - sx, sb - sy, tr - tx, tb - ty
        w = min(max(int((r - l) * tw / sw_), 200), tw)
        h = min(max(int((b - t) * th / sh_), 150), th)
        x = tx + int((l - sx) * tw / sw_)
        y = ty + int((t - sy) * th / sh_)
        x = max(tx, min(x, tr - w))
        y = max(ty, min(y, tb - h))
        _place(hwnd, x, y, w, h)
        if was_max:
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        return f"Перенёс окно {name} с монитора {current.number} на монитор {target.number}."
    raise ValueError(f"unknown action {action!r}")


class WindowControlTool:
    name: str = "window_control"
    risk_tier: str = "monitor"
    description: str = (
        "Moves and arranges windows. action: move_to_monitor (window=program/title fragment or empty for the "
        "active window, monitor=1|2|3|next), snap (side=left|right|top|bottom|full), center, maximize, minimize, "
        "restore, list_monitors, list_windows. Monitors are numbered as in Windows Settings."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS},
            "window": {"type": "string", "description": "Program or title fragment; empty = active window."},
            "monitor": {"type": "string", "description": "Target monitor number (1, 2, ...) or 'next'."},
            "side": {"type": "string", "enum": list(_SNAPS)},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:  # noqa: ARG002
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="window_control works on Windows only")
        action = str(args.get("action") or "").strip()
        if action not in _ACTIONS:
            return ToolResult(success=False, output=None, error=f"unknown action {action!r}; use one of {_ACTIONS}")
        try:
            text = await asyncio.to_thread(_do, action, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("window_control %s failed", action, exc_info=True)
            return ToolResult(success=False, output=None, error=f"window_control {action}: {exc}")
        return ToolResult(success=True, output=text)
