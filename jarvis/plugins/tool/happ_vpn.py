"""happ_vpn: run Happ's own latency test and connect to the fastest server.

Fork addition (2026-11-14). Happ (a Qt/QML VPN client) has no CLI or API, so the
tool drives its window:

1. bring the Happ window up (start Happ if it is not running), maximize it so the
   whole server list is visible;
2. press the "test all servers" button through UI Automation (Invoke);
3. read every row's latency from the screen with the Windows OCR engine (the
   numbers are drawn, not exposed as UI text), waiting until the readings settle;
4. select the row with the lowest latency and make sure the tunnel is up. The
   tunnel state is read from the network adapter Happ creates (``happ-xray``),
   never from a guess.

The window is put back to its previous size and the previously active window is
re-focused afterwards. Windows only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform
from jarvis.plugins.tool import ui_helpers as ui

logger = logging.getLogger(__name__)

_PROC = "Happ"
_DIGIT_FIX = str.maketrans({"b": "8", "s": "5", "o": "0", "l": "1", "i": "1", "z": "2", "g": "6", "q": "9"})
_ACTIONS = ["status", "ping", "connect_fastest", "connect"]


def parse_ms(text: str) -> int | None:
    """'193ms' -> 193; tolerant of the OCR slips seen on small grey text ('19Bms', '3SBms')."""
    t = text.strip().lower().replace(" ", "")
    if not t.endswith("ms"):
        return None
    digits = t[:-2].translate(_DIGIT_FIX)
    return int(digits) if digits.isdigit() and 0 < int(digits) < 20000 else None


def tunnel_up() -> bool:
    """True when Happ's TUN adapter (named like 'happ-xray') is up."""
    import psutil

    stats = psutil.net_if_stats()
    return any(st.isup and ("happ" in name.lower() or "xray" in name.lower()) for name, st in stats.items())


def _ensure_window() -> ui.Win:
    win = ui.find_window(_PROC, "Happ")
    if win is not None:
        return win
    import psutil

    exe = next((p.info["exe"] for p in psutil.process_iter(["name", "exe"])
                if (p.info["name"] or "").lower() == "happ.exe" and p.info["exe"]), None)
    if exe is None:
        from jarvis.plugins.tool.app_resolver import resolve_app_launch_target

        target = resolve_app_launch_target("happ")
        if target.kind == "startfile":
            os.startfile(target.value)  # type: ignore[attr-defined]  # noqa: S606 — launching the user's own app
        else:
            subprocess.Popen([target.value], shell=False)  # noqa: S603
    else:
        subprocess.Popen([exe], shell=False)  # noqa: S603 — a second launch raises the existing window
    for _ in range(25):
        time.sleep(0.8)
        win = ui.find_window(_PROC, "Happ")
        if win is not None:
            return win
    raise RuntimeError("окно Happ не появилось")


def _layout() -> dict[str, Any]:
    els = ui.uia_elements(_PROC, "Happ")
    rows = [e for e in els if e["id"].endswith("ServerItem_QMLTYPE_45") and 36 <= e["height"] <= 64]
    rows.sort(key=lambda e: e["top"])
    edit = next((e for e in els if e["type"] == "Edit"), None)
    buttons = [e for e in els if e["type"] == "Button"]
    ping_all = None
    if edit is not None:
        near = [b for b in buttons if abs(b["top"] - (edit["top"] + 10)) <= 8 and b["width"] <= 30]
        near.sort(key=lambda b: b["left"])
        ping_all = near[0] if near else None
    test_button = next((b for b in buttons if "пинга" in (b["name"] or "").lower() or b["id"].endswith("Button_QMLTYPE_17")), None)
    return {"rows": rows, "ping_all": ping_all, "test_button": test_button}


def _read_pings(win: ui.Win, rows: list[dict[str, Any]]) -> dict[int, int | None]:
    """Latency per row index, read from the screen in one OCR pass."""
    left, top, width, height = ui.window_rect(win.handle)
    img = ui.capture(left, top, width, height)
    x0 = max(0, rows[0]["left"] + rows[0]["width"] - 92 - left)
    x1 = min(width, rows[0]["left"] + rows[0]["width"] + 4 - left)
    y0 = max(0, rows[0]["top"] - top)
    y1 = min(height, rows[-1]["top"] + rows[-1]["height"] - top)
    if x1 <= x0 or y1 <= y0:
        return {}
    words = ui.ocr(img.crop((x0, y0, x1, y1)), lang="en-US", scale=5, invert=True)
    result: dict[int, int | None] = {}
    for i, row in enumerate(rows):
        r0, r1 = row["top"] - top - y0, row["top"] + row["height"] - top - y0
        hit = [w for w in words if r0 <= w.cy < r1]
        result[i] = next((v for v in (parse_ms(w.text) for w in hit) if v is not None), None)
    return result


def _read_names(win: ui.Win, rows: list[dict[str, Any]]) -> dict[int, str]:
    left, top, width, height = ui.window_rect(win.handle)
    img = ui.capture(left, top, width, height)
    x0 = max(0, rows[0]["left"] + 40 - left)
    x1 = min(width, rows[0]["left"] + rows[0]["width"] - 95 - left)
    y0 = max(0, rows[0]["top"] - top)
    y1 = min(height, rows[-1]["top"] + rows[-1]["height"] - top)
    words = ui.ocr(img.crop((x0, y0, x1, y1)), lang="ru", scale=2)
    names: dict[int, str] = {}
    for i, row in enumerate(rows):
        r0, r1 = row["top"] - top - y0, row["top"] + row["height"] - top - y0
        # the first (bigger) line of the row is the server name; the second is the protocol
        top_line = [w for w in words if r0 <= w.cy < r0 + (r1 - r0) * 0.55]
        names[i] = " ".join(w.text for w in sorted(top_line, key=lambda w: w.x)) or f"сервер {i + 1}"
    return names


def _ms(valid: dict[int, int], index: int) -> str:
    return f" ({valid[index]} мс)" if index in valid else ""


def _selected_row(win: ui.Win, rows: list[dict[str, Any]]) -> int | None:
    """Index of the highlighted row: it carries a purple bar on its left edge."""
    left, top, width, height = ui.window_rect(win.handle)
    img = ui.capture(left, top, width, height)
    for i, row in enumerate(rows):
        x = row["left"] - left
        cy = row["top"] + row["height"] // 2 - top
        if not (0 <= cy < height - 4):
            continue
        hits = 0
        for dx in range(0, 6):
            for dy in range(-12, 13, 2):
                px, py = x + dx, cy + dy
                if 0 <= px < width and 0 <= py < height:
                    r, g, b = img.getpixel((px, py))
                    if b > 190 and 80 < r < 190 and g < 150:
                        hits += 1
        if hits >= 8:
            return i
    return None


def _run(action: str, server: str = "") -> str:  # noqa: C901 — one linear automation flow
    connected = tunnel_up()
    if action == "status":
        return "Happ: VPN подключён (туннель happ-xray активен)." if connected else "Happ: VPN отключён."
    previous = ui.foreground()
    win = _ensure_window()
    was_minimized = win.minimized
    try:
        if not ui.focus(win, settle=1.0):
            raise RuntimeError("не удалось вывести Happ на передний план")
        ui.uia_window_state(_PROC, "Maximized", "Happ")
        time.sleep(1.2)
        win = ui.find_window(_PROC, "Happ") or win
        layout = _layout()
        rows, ping_all = layout["rows"], layout["ping_all"]
        if not rows:
            raise RuntimeError("не вижу список серверов Happ (открыта вкладка «Серверы»?)")
        if ping_all is None:
            raise RuntimeError("не нашёл кнопку проверки пинга всех серверов")
        if not ui.uia_invoke(_PROC, ping_all["left"], ping_all["top"], "Happ"):
            raise RuntimeError("кнопка проверки пинга не нажалась")
        previous_reading: dict[int, int | None] = {}
        readings: dict[int, int | None] = {}
        stable = 0
        deadline = time.monotonic() + 50
        time.sleep(6.0)
        while time.monotonic() < deadline:
            readings = _read_pings(win, rows)
            if readings and readings == previous_reading and any(v is not None for v in readings.values()):
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            previous_reading = readings
            time.sleep(3.0)
        valid = {i: v for i, v in readings.items() if v is not None}
        if not valid:
            raise RuntimeError("результаты пинга не появились на экране (или не распознались)")
        names = _read_names(win, rows)
        best = min(valid, key=lambda i: valid[i])
        lines = [f"- {names[i]}: " + (f"{readings[i]} мс" if readings[i] is not None else "нет данных") for i in range(len(rows))]
        table = "Пинг серверов Happ:\n" + "\n".join(lines)
        if len(valid) < len(rows):
            table += f"\n(видно {len(valid)} из {len(rows)} значений)"
        if action == "ping":
            return table + f"\nЛучший: {names[best]}, {valid[best]} мс."
        target, label = best, "самому быстрому"
        if action == "connect":
            wanted = server.strip().lower().replace("ё", "е")
            matches = [i for i in range(len(rows)) if wanted and wanted in names[i].lower().replace("ё", "е")]
            if not matches:
                raise RuntimeError(table + f"\nСервера с названием «{server}» в списке нет.")
            target, label = matches[0], "выбранному"
        selected = _selected_row(win, rows)
        if selected == target and connected:
            ms = f", {valid[target]} мс" if target in valid else ""
            return table + f"\nУже подключён к {label} серверу: {names[target]}{ms}."
        # select the best row: UI Automation invoke first, a real click as the fallback
        row = rows[target]
        ui.uia_invoke(_PROC, row["left"], row["top"], "Happ")
        time.sleep(1.5)
        if _selected_row(win, rows) != target:
            ui.click(row["left"] + 120, row["top"] + 14)
            time.sleep(1.5)
        if _selected_row(win, rows) != target:
            raise RuntimeError(table + f"\nНе смог выбрать сервер «{names[target]}» в списке.")
        ui.uia_window_state(_PROC, "Normal", "Happ")
        time.sleep(1.0)
        # a switch while connected normally reconnects by itself; wait and look at the tunnel
        for _ in range(10):
            time.sleep(1.0)
            if tunnel_up():
                return table + f"\nВыбран {names[target]}{_ms(valid, target)}, VPN подключён."
        layout = _layout()
        test_button = layout["test_button"]
        if test_button is None:
            raise RuntimeError(table + f"\nСервер «{names[target]}» выбран, но кнопку подключения не нашёл.")
        # the round power button sits above the "Тест пинга" button in the right panel
        ui.click(test_button["left"] + test_button["width"] // 2 - 27, test_button["top"] - 441)
        for _ in range(14):
            time.sleep(1.0)
            if tunnel_up():
                return table + f"\nВыбран {names[target]}{_ms(valid, target)}, нажал подключение: VPN подключён."
        raise RuntimeError(table + f"\nСервер «{names[target]}» выбран, но туннель не поднялся: подключитесь вручную.")
    finally:
        try:
            ui.uia_window_state(_PROC, "Minimized" if was_minimized else "Normal", "Happ")
        except Exception:  # noqa: BLE001
            pass
        ui.restore_focus(previous)


class HappVpnTool:
    name: str = "happ_vpn"
    risk_tier: str = "monitor"
    description: str = (
        "Controls the Happ VPN client. action: status (is the VPN tunnel up), "
        "ping (run Happ's latency test and report every server), connect_fastest "
        "(run the test, then select and connect to the server with the lowest "
        "latency), connect (connect to the server whose name contains `server`, "
        "e.g. 'Чехия'). It reads real latencies from the Happ window."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS, "default": "status"},
            "server": {"type": "string", "description": "Name part for action=connect"},
        },
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:
        return "ask" if str(args.get("action") or "") in {"connect_fastest", "connect"} else "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="happ_vpn supports Windows only")
        action = str(args.get("action") or "status").lower()
        if action not in _ACTIONS:
            return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
        logger.info("[HAPP] action=%s trace=%s", action, ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_run, action, str(args.get("server") or "")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HAPP] %s failed: %s", action, exc)
            return ToolResult(success=False, output=None, error=f"Happ: {exc}")


