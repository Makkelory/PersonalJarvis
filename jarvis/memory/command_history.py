"""command_history: what the user asked for and when (``data/command_history.jsonl``).

Fork addition (2026-11-14). Feeds "что я просил вчера" and the memory tool.
Append-only, one JSON object per line; never raises.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

_PATH = Path("data") / "command_history.jsonl"
_SKIP_TOOLS = {"memory_notes", "app_catalog"}


def record(utterance: str, tools: list[str]) -> None:
    if not utterance.strip() or set(tools) <= _SKIP_TOOLS:
        return
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        row = {"t": time.time(), "text": utterance.strip()[:300], "tools": tools}
        with _PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def on_day(days_back: int = 0, limit: int = 40) -> list[dict]:
    """Rows from the calendar day ``days_back`` days ago (0 = today), oldest first."""
    day = (datetime.now() - timedelta(days=days_back)).date()
    rows: list[dict] = []
    try:
        for line in _PATH.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if datetime.fromtimestamp(row.get("t", 0)).date() == day:
                rows.append(row)
    except OSError:
        return []
    return rows[-limit:]


def summary(days_back: int = 0) -> str:
    rows = on_day(days_back)
    label = {0: "сегодня", 1: "вчера"}.get(days_back, f"{days_back} дн. назад")
    if not rows:
        return f"За {label} команд в журнале нет."
    lines = [f"{datetime.fromtimestamp(r['t']):%H:%M} — {r['text']}" for r in rows]
    return f"Команды за {label} ({len(rows)}):\n" + "\n".join(lines)
