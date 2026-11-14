"""Russian-locale window titles must match the English app token (2026-11-14).

On a Russian Windows the Notepad title is "Без имени — Блокнот", which does not
contain "notepad". Without an alias the raise poll never found the window.
"""

from __future__ import annotations

import pytest

from jarvis.platform.window_state import WindowInfo, _match_window


def _win(title: str) -> WindowInfo:
    return WindowInfo(title=title, handle=1)


@pytest.mark.parametrize(
    ("app", "title"),
    [
        ("notepad", "Без имени — Блокнот"),
        ("notepad", "Документ.txt — Блокнот"),
        ("calc", "Калькулятор"),
        ("explorer", "Проводник"),
        ("taskmgr", "Диспетчер задач"),
    ],
)
def test_russian_title_matches_english_token(app: str, title: str) -> None:
    assert _match_window(app, [_win(title)]) is not None


def test_unrelated_window_is_not_matched() -> None:
    assert _match_window("notepad", [_win("Telegram")]) is None
