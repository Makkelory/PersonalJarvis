"""Deterministic plans for 'what runs / what is installed' (fork addition)."""

from __future__ import annotations

import pytest

from jarvis.brain.local_action_gate import match_local_action

_LIVE = ("open_app", "list_apps")


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("Какие программы запущены?", "running"),
        ("Джарвис, какие программы сейчас запущены", "running"),
        ("что запущено", "running"),
        ("Покажи запущенные процессы", "running"),
        ("Какие приложения установлены на компьютере", "installed"),
        ("Просканируй диски на наличие приложений", "installed"),
    ],
)
def test_listing_phrases_route_to_list_apps(text: str, scope: str) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert [c.name for c in plan.tool_calls] == ["list_apps"]
    assert plan.tool_calls[0].args["scope"] == scope


def test_scan_request_forces_a_rescan() -> None:
    plan = match_local_action("Просканируй диски", live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].args["rescan"] is True


def test_find_app_passes_the_query() -> None:
    plan = match_local_action("Найди приложение Steam", live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].args == {"scope": "installed", "query": "steam"}


@pytest.mark.parametrize(
    "text",
    [
        "Привет, как дела",
        "Какие программы можно использовать для монтажа видео",
        "Что запущено в Хабаровске",
        "Расскажи про процессы в организме",
        "Найди в интернете приложение для заметок",
    ],
)
def test_ordinary_conversation_is_left_alone(text: str) -> None:
    assert match_local_action(text, live_tool_names=_LIVE) is None


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("Громкость 50", "set_volume", {"action": "set", "level": 50}),
        ("Джарвис, сделай громче", "set_volume", {"action": "up"}),
        ("потише", "set_volume", {"action": "down"}),
        ("выключи звук", "set_volume", {"action": "mute"}),
        ("включи звук", "set_volume", {"action": "unmute"}),
        ("Какое состояние компьютера", "system_status", {}),
        ("сколько свободно места на дисках", "system_status", {}),
        ("Закрой блокнот", "close_app", {"name": "notepad"}),
        ("закрой стим", "close_app", {"name": "steam"}),
    ],
)
def test_system_control_phrases(text: str, tool: str, args: dict) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].name == tool
    assert dict(plan.tool_calls[0].args) == args


@pytest.mark.parametrize("text", ["Громкость 150", "закрой дверь", "закрой балокно", "расскажи про звук"])
def test_system_control_ignores_unrelated_or_invalid(text: str) -> None:
    assert match_local_action(text, live_tool_names=_LIVE) is None


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("Сделай скриншот", "pc_control", {"action": "screenshot"}),
        ("заблокируй компьютер", "pc_control", {"action": "lock"}),
        ("выключи компьютер", "pc_control", {"action": "shutdown"}),
        ("отмени выключение", "pc_control", {"action": "shutdown_cancel"}),
        ("сверни все окна", "pc_control", {"action": "show_desktop"}),
        ("закрой окно", "pc_control", {"action": "close_window"}),
        ("пауза", "pc_control", {"action": "play_pause"}),
        ("следующий трек", "pc_control", {"action": "next"}),
        ("который час", "pc_control", {"action": "time"}),
        ("проверь интернет", "network_status", {}),
        ("открой загрузки", "file_ops", {"action": "open", "path": "загрузки"}),
        ("найди файл договор", "file_ops", {"action": "find", "query": "договор"}),
        ("открой настройки звука", "pc_control", {"action": "open_settings", "page": "sound"}),
    ],
)
def test_everyday_pc_phrases(text: str, tool: str, args: dict) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].name == tool
    assert dict(plan.tool_calls[0].args) == args


@pytest.mark.parametrize(
    "text",
    ["пауза в разговоре была долгой", "скриншот был плохой", "закрой окно в кухне", "выключи свет", "который час сейчас"],
)
def test_everyday_pc_patterns_are_anchored(text: str) -> None:
    assert match_local_action(text, live_tool_names=_LIVE) is None


@pytest.mark.parametrize(
    ("text", "args"),
    [
        ("уменьши громкость до 50%", {"action": "set", "level": 50}),
        ("Громкость звука на пк установи 50%", {"action": "set", "level": 50}),
        ("увеличь громкость на 20", {"action": "up", "step": 20}),
        ("убавь звук на 10 процентов", {"action": "down", "step": 10}),
        ("поставь звук на пятьдесят процентов", {"action": "set", "level": 50}),
    ],
)
def test_free_word_volume_commands(text: str, args: dict) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].name == "set_volume"
    assert dict(plan.tool_calls[0].args) == args


@pytest.mark.parametrize("text", ["Громкость 150", "какая громкость сейчас 50", "расскажи про звук 5.1 в кино и как его настроить"])
def test_free_word_volume_ignores_questions_and_out_of_range(text: str) -> None:
    assert match_local_action(text, live_tool_names=_LIVE) is None
