"""app_control: operate ANY program through its interface, like a user would.

Fork addition (2026-11-14). Three layers, tried in this order:

1. **UI Automation** (Win32, WinForms, WPF, Qt/QML, UWP): lists real controls
   (buttons, menu items, tabs, fields) by name and invokes them.
2. **OCR** (Windows.Media.Ocr, Russian + English): for programs without usable
   accessibility (Chromium/Electron apps such as Spotify, Discord, Steam): finds a
   visible word on the window and clicks it.
3. **Keyboard**: hotkeys and typing into the focused window (text goes through
   the clipboard so Cyrillic works; the old clipboard is restored).

Everything reports what really happened (which layer, found or not): no guessing.
Destructive control names (delete, format, reset ...) are refused unless the call
carries ``confirm: true``, which only an explicit owner request may set.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

_ACTIONS = ["inspect", "read", "click", "type", "hotkey", "focus"]
_USEFUL = {"Button", "MenuItem", "TabItem", "Edit", "CheckBox", "ComboBox", "ListItem", "Hyperlink", "RadioButton",
           "Slider", "SplitButton", "TreeItem", "Menu", "MenuBar", "Document"}
_DESTRUCTIVE = re.compile(
    r"удал|стереть|очист|формат|сброс|сбросить|деинстал|uninstall|delete|remove|erase|format|reset|wipe|clear all|"
    r"выключ.* компьютер|shut ?down|restart|перезагруз",
    re.I,
)
_VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B,
       "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
       "pgup": 0x21, "pgdn": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "insert": 0x2D}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
_VK.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})
_VK.update({"plus": 0xBB, "minus": 0xBD, "+": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE, "/": 0xBF})


def _window(program: str):
    from jarvis.plugins.tool import window_control as wc  # noqa: PLC0415

    picked = wc._pick_window(program)  # noqa: SLF001
    return picked  # (hwnd, title, process) or None


def _focus(hwnd: int) -> None:
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    ui.focus(ui.Win(hwnd, "", 0, False), settle=0.5)


def _elements(proc: str) -> list[dict[str, Any]]:
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    try:
        rows = ui.uia_elements(proc, "")
    except Exception:  # noqa: BLE001 - a program without UIA is handled by OCR
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        name = str(r.get("name") or "").strip()
        kind = str(r.get("type") or "")
        unnamed = False
        if not name:
            # Qt/QML programs name their controls only by AutomationId ("menuButton", "NavButton_QMLTYPE_3").
            raw_id = re.sub(r"_QMLTYPE_\d+(_QML_\d+)?", "", str(r.get("id") or "")).strip("._ ")
            name, unnamed = raw_id, True
        if not name or (kind not in _USEFUL and not unnamed) or (kind, name) in seen:
            continue
        if unnamed and kind not in _USEFUL and kind not in {"Group", "ProgressBar"}:
            continue
        seen.add((kind, name))
        out.append({**r, "name": name, "unnamed": unnamed})
    return out


def _ocr_words(hwnd: int):
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    left, top, width, height = ui.window_rect(hwnd)
    img = ui.capture(left, top, width, height)
    words = []
    for lang in ("ru", "en-US"):
        try:
            words.extend(ui.ocr(img, lang=lang, scale=2))
        except Exception:  # noqa: BLE001 - one missing language pack must not kill the other
            continue
    return words, (left, top)


def _inspect(program: str) -> str:
    picked = _window(program)
    if picked is None:
        return f"Окно «{program}» не вижу: программа не открыта. Сначала откройте её."
    hwnd, title, proc = picked
    _focus(hwnd)
    els = _elements(proc)
    named = [e for e in els if not e.get("unnamed")]
    if len(named) >= 3:
        lines = [f"{e['type']}: {e['name']}" for e in els[:70]]
        return f"Окно «{title[:50]}» ({proc}), элементы интерфейса (UI Automation, {len(els)}):\n" + "\n".join(lines)
    ids = [f"{e['type']} [{e['name']}]" for e in els[:25]]
    words, _origin = _ocr_words(hwnd)
    texts = []
    for w in words:
        if len(w.text) > 1 and w.text not in texts:
            texts.append(w.text)
    if not texts and not ids:
        return f"В окне «{title[:50]}» ({proc}) не нашёл ни элементов, ни читаемого текста."
    note = ""
    if ids:
        note = "Элементы без названий (по AutomationId): " + ", ".join(ids) + ". "
    return (f"Окно «{title[:50]}» ({proc}): названий у элементов мало, поэтому смотрю экран. {note}"
            "Надписи на экране (OCR): " + ", ".join(texts[:80]))


def _read(program: str) -> str:
    picked = _window(program)
    if picked is None:
        return f"Окно «{program}» не вижу."
    hwnd, title, proc = picked
    _focus(hwnd)
    words, _o = _ocr_words(hwnd)
    text = " ".join(w.text for w in words)
    return f"Текст в окне «{title[:50]}»: {text[:1500]}" if text else "Читаемого текста в окне нет."


def _click(program: str, element: str, confirm: bool) -> str:
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    if _DESTRUCTIVE.search(element) and not confirm:
        return (f"Элемент «{element}» выглядит опасным (удаление, сброс, форматирование). Без прямой просьбы Сэра "
                "не нажимаю: переспросите его и повторите с подтверждением.")
    picked = _window(program)
    if picked is None:
        return f"Окно «{program}» не вижу: сначала откройте программу."
    hwnd, title, proc = picked
    _focus(hwnd)
    wanted = element.lower().strip()
    els = _elements(proc)
    match = next((e for e in els if e["name"].lower() == wanted), None) or next(
        (e for e in els if wanted in e["name"].lower()), None)
    if match is not None:
        if _DESTRUCTIVE.search(match["name"]) and not confirm:
            return f"Найденный элемент «{match['name']}» опасен: не нажимаю без подтверждения."
        if ui.uia_invoke(proc, match["left"], match["top"], ""):
            return f"Нажал «{match['name']}» ({match['type']}) в «{title[:40]}» через UI Automation."
        ui.click(match["left"] + match["width"] // 2, match["top"] + match["height"] // 2)
        return f"Кликнул по «{match['name']}» ({match['type']}) в «{title[:40]}» по координатам."
    words, (ox, oy) = _ocr_words(hwnd)
    parts = wanted.split()
    for w in words:
        if w.text.lower() == parts[0] or (len(parts[0]) > 3 and parts[0] in w.text.lower()):
            ui.click(ox + w.cx, oy + w.cy)
            return f"Нашёл надпись «{w.text}» на экране и кликнул по ней (OCR). Проверьте результат."
    return f"В окне «{title[:40]}» не нашёл «{element}» ни среди элементов, ни на экране."


def _type(program: str, text: str, element: str) -> str:
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    picked = _window(program)
    if picked is None:
        return f"Окно «{program}» не вижу."
    hwnd, title, _proc = picked
    _focus(hwnd)
    if element:
        res = _click(program, element, False)
        if not res.startswith(("Нажал", "Кликнул", "Нашёл надпись")):
            return res
        time.sleep(0.3)
    old = ""
    try:
        old = ui.run_ps("Get-Clipboard -Raw", timeout=10)
    except Exception:  # noqa: BLE001
        old = ""
    escaped = text.replace("'", "''")
    ui.run_ps(f"Set-Clipboard -Value '{escaped}'", timeout=10)
    ui.chord(0x11, ord("V"))
    time.sleep(0.4)
    if old:
        try:
            ui.run_ps("Set-Clipboard -Value '" + old.rstrip("\r\n").replace("'", "''") + "'", timeout=10)
        except Exception:  # noqa: BLE001
            pass
    return f"Ввёл текст в «{title[:40]}»" + (f" (поле «{element}»)" if element else "") + "."


def _hotkey(program: str, keys: str) -> str:
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    parts = [p.strip().lower() for p in re.split(r"[+\s]+", keys) if p.strip()]
    codes = [_VK.get(p) for p in parts]
    if not parts or any(c is None for c in codes):
        return f"Не понял сочетание «{keys}». Пример: ctrl+shift+t."
    if program:
        picked = _window(program)
        if picked is None:
            return f"Окно «{program}» не вижу."
        _focus(picked[0])
    ui.chord(*codes)  # type: ignore[arg-type]
    return f"Нажал {'+'.join(parts)}" + (f" в «{program}»." if program else ".")


def _do(action: str, args: dict[str, Any]) -> str:
    program = str(args.get("program") or args.get("window") or "").strip()
    if action == "hotkey":
        return _hotkey(program, str(args.get("keys") or ""))
    if not program:
        return "Укажите программу."
    if action == "inspect":
        return _inspect(program)
    if action == "read":
        return _read(program)
    if action == "focus":
        picked = _window(program)
        if picked is None:
            return f"Окно «{program}» не вижу."
        _focus(picked[0])
        return f"Переключился на «{picked[1][:50]}»."
    if action == "click":
        element = str(args.get("element") or "").strip()
        return _click(program, element, bool(args.get("confirm"))) if element else "Какой элемент нажать?"
    if action == "type":
        text = str(args.get("text") or "")
        return _type(program, text, str(args.get("element") or "").strip()) if text else "Что ввести?"
    return f"Неизвестное действие {action!r}."


class AppControlTool:
    name: str = "app_control"
    risk_tier: str = "monitor"
    description: str = (
        "Operate ANY open program through its interface. action: inspect (program: lists its real buttons, menus, "
        "tabs and fields, or the on-screen words when the program has no accessibility), read (visible text), "
        "click (program, element: name of a control or a visible word), type (program, text, element=field name), "
        "hotkey (keys like 'ctrl+shift+t', program optional), focus. Always inspect first, then click by the exact "
        "name. Reports honestly what it found. Never pass confirm=true unless the owner explicitly asked for a "
        "destructive control (delete, reset, format)."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _ACTIONS},
            "program": {"type": "string", "description": "Program name (Russian is fine) or window title fragment."},
            "element": {"type": "string"}, "text": {"type": "string"}, "keys": {"type": "string"},
            "confirm": {"type": "boolean"},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:  # noqa: ARG002
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="app_control works on Windows only")
        action = str(args.get("action") or "")
        if action not in _ACTIONS:
            return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
        try:
            text = await asyncio.to_thread(_do, action, args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("app_control %s failed", action, exc_info=True)
            return ToolResult(success=False, output=None, error=f"app_control {action}: {exc}")
        return ToolResult(success=True, output=text)
