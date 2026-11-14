"""set_timer: timers and reminders that speak when they fire.

Fork addition (2026-11-14). ``set_timer`` starts a countdown (``minutes`` /
``seconds``) or a wall-clock reminder (``at`` = ``HH:MM``), keeps it in
``data/timers.json`` so it survives a Jarvis restart, and when it fires it
(1) publishes an ``AnnouncementRequested`` (spoken through the normal TTS
pipeline, interrupting current speech) and (2) plays a Windows alert sound as
a fallback for muted or broken speech.

The bus is bound once at boot by ``bind`` (called from the local-action tool
loader); until then the fallback sound still fires. ``threading.Timer`` is used
instead of asyncio tasks because tool calls may run on short-lived event loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_STORE = Path("data") / "timers.json"
_LOCK = threading.RLock()
_BUS: Any = None
_LOOP: asyncio.AbstractEventLoop | None = None
_TIMERS: dict[int, dict[str, Any]] = {}  # id -> {"id", "due", "label", "timer"}
_NEXT_ID = 1
_MAX_SECONDS = 24 * 3600


def bind(bus: Any) -> None:
    """Remember the event bus (and the running loop, if any) and restore saved timers."""
    global _BUS, _LOOP
    _BUS = bus
    try:
        _LOOP = asyncio.get_running_loop()
    except RuntimeError:
        _LOOP = None
    restore()


def _save() -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        data = [{"id": t["id"], "due": t["due"], "label": t["label"]} for t in _TIMERS.values()]
        _STORE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.debug("[TIMER] could not persist timers", exc_info=True)


def _spoken(label: str) -> str:
    return f"Сэр, время вышло: {label}." if label else "Сэр, таймер сработал."


def _announce(text: str) -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:  # noqa: BLE001 — a missing sound must never block the message
        pass
    bus = _BUS
    if bus is None:
        logger.warning("[TIMER] fired but no bus is bound; only the alert sound played")
        return
    from jarvis.core.events import AnnouncementRequested

    event = AnnouncementRequested(text=text, priority="interrupt", language="ru", kind="info")
    try:
        loop = _LOOP
        if loop is not None and not loop.is_closed() and loop.is_running():
            asyncio.run_coroutine_threadsafe(bus.publish(event), loop)
        else:
            asyncio.run(bus.publish(event))
    except Exception:  # noqa: BLE001
        logger.warning("[TIMER] announcement failed", exc_info=True)


def _fire(timer_id: int) -> None:
    with _LOCK:
        entry = _TIMERS.pop(timer_id, None)
        _save()
    if entry is None:
        return
    logger.info("[TIMER] fired id=%s label=%r", timer_id, entry["label"])
    _announce(_spoken(entry["label"]))


def _schedule(timer_id: int, due: float, label: str) -> None:
    delay = max(0.0, due - time.time())
    timer = threading.Timer(delay, _fire, args=(timer_id,))
    timer.daemon = True
    with _LOCK:
        _TIMERS[timer_id] = {"id": timer_id, "due": due, "label": label, "timer": timer}
        _save()
    timer.start()


def restore() -> int:
    """Re-arm timers saved by a previous run; overdue ones fire right away."""
    global _NEXT_ID
    try:
        saved = json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    count = 0
    with _LOCK:
        for item in saved:
            tid = int(item["id"])
            if tid in _TIMERS:
                continue
            _NEXT_ID = max(_NEXT_ID, tid + 1)
            _schedule(tid, float(item["due"]), str(item.get("label") or ""))
            count += 1
    return count


def _fmt_remaining(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = ([f"{h} ч"] if h else []) + ([f"{m} мин"] if m else []) + ([f"{s} с"] if s and not h else [])
    return " ".join(parts) or "0 с"


def _do(args: dict[str, Any]) -> str:
    global _NEXT_ID
    action = str(args.get("action") or "set").lower()
    if action == "list":
        with _LOCK:
            items = sorted(_TIMERS.values(), key=lambda t: t["due"])
        if not items:
            return "Активных таймеров и напоминаний нет."
        lines = [f"Активно: {len(items)}."]
        for t in items:
            when = datetime.fromtimestamp(t["due"]).strftime("%H:%M:%S")
            lines.append(f"- №{t['id']}: через {_fmt_remaining(t['due'] - time.time())} (в {when})"
                         + (f", «{t['label']}»" if t["label"] else ""))
        return "\n".join(lines)
    if action == "cancel":
        with _LOCK:
            wanted = args.get("id")
            ids = [int(wanted)] if wanted not in (None, "", "all") else list(_TIMERS)
            cancelled = 0
            for tid in ids:
                entry = _TIMERS.pop(tid, None)
                if entry is not None:
                    entry["timer"].cancel()
                    cancelled += 1
            _save()
        return f"Отменено: {cancelled}." if cancelled else "Нечего отменять: активных таймеров нет."
    if action != "set":
        raise ValueError(f"unknown action {action!r}")
    seconds = float(args.get("seconds") or 0) + 60.0 * float(args.get("minutes") or 0) + 3600.0 * float(args.get("hours") or 0)
    at = str(args.get("at") or "").strip()
    if at:
        try:
            hh, mm = (int(x) for x in at.replace(".", ":").split(":")[:2])
        except ValueError as exc:
            raise ValueError("время задаётся как ЧЧ:ММ") from exc
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("время вне диапазона 00:00–23:59")
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        seconds = (target - now).total_seconds()
    if seconds < 1:
        raise ValueError("укажи, через сколько минут или в какое время")
    if seconds > _MAX_SECONDS:
        raise ValueError("максимум 24 часа")
    label = str(args.get("label") or "").strip()[:120]
    with _LOCK:
        timer_id = _NEXT_ID
        _NEXT_ID += 1
    _schedule(timer_id, time.time() + seconds, label)
    when = datetime.fromtimestamp(time.time() + seconds).strftime("%H:%M")
    suffix = f" — «{label}»" if label else ""
    return f"Поставил таймер №{timer_id}: через {_fmt_remaining(seconds)} (в {when}){suffix}."


class SetTimerTool:
    name: str = "set_timer"
    risk_tier: str = "safe"
    description: str = (
        "Timers and reminders that speak aloud when they fire. action: set "
        "(minutes / seconds / hours, or at='HH:MM'; optional label = what to "
        "remind), list, cancel (optional id; none = all). Max 24 hours."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "list", "cancel"], "default": "set"},
            "minutes": {"type": "number"}, "seconds": {"type": "number"}, "hours": {"type": "number"},
            "at": {"type": "string", "description": "Wall-clock time HH:MM (today or tomorrow)"},
            "label": {"type": "string", "description": "What to remind about"},
            "id": {"type": "integer", "description": "Timer number for cancel"},
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        logger.info("[TIMER] args=%s trace=%s", {k: v for k, v in args.items() if k != "label"}, ctx.trace_id)
        try:
            return ToolResult(success=True, output=await asyncio.to_thread(_do, dict(args)))
        except (ValueError, TypeError) as exc:
            return ToolResult(success=False, output=None, error=f"Не получилось: {exc}")
