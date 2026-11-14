"""Browser, named windows, layouts, memory and scenes through the deterministic gate (fork addition)."""
from __future__ import annotations

import pytest

from jarvis.brain.local_action_gate import match_local_action


def _call(phrase: str) -> tuple[str, dict]:
    plan = match_local_action(phrase)
    assert plan is not None, phrase
    return plan.tool_calls[0].name, dict(plan.tool_calls[0].args)


@pytest.mark.parametrize("phrase,expected", [
    ("открой ютуб", ("browser_control", {"action": "open_url", "site": "ютуб"})),
    ("загугли погода в москве", ("browser_control", {"action": "search", "query": "погода в москве", "engine": "google"})),
    ("найди на ютубе лоуфи", ("browser_control", {"action": "search", "query": "лоуфи", "engine": "youtube"})),
    ("закрой вкладку", ("browser_control", {"action": "close_tab"})),
    ("следующая вкладка", ("browser_control", {"action": "next_tab"})),
    ("сверни телеграм", ("window_control", {"action": "minimize", "window": "телеграм"})),
    ("переключись на дискорд", ("window_control", {"action": "focus", "window": "дискорд"})),
    ("расположи окна рядом", ("window_control", {"action": "tile", "count": 2})),
    ("запомни расположение окон как работа", ("window_control", {"action": "save_layout", "name": "работа"})),
    ("запомни, что я люблю инди рок", ("memory_notes", {"action": "remember", "text": "я люблю инди рок"})),
    ("что ты помнишь про жанр", ("memory_notes", {"action": "recall", "query": "жанр"})),
    ("что я просил вчера", ("memory_notes", {"action": "history", "days_back": 1})),
    ("включи игровой режим", ("scenes", {"action": "run", "name": "игровой"})),
])
def test_new_skill_phrases(phrase: str, expected: tuple[str, dict]) -> None:
    assert _call(phrase) == expected


def test_show_all_windows_still_goes_to_pc_control() -> None:
    assert _call("сверни все окна")[0] == "pc_control"


def test_an_unknown_scene_is_not_hijacked() -> None:
    assert match_local_action("включи режим кино") is None
