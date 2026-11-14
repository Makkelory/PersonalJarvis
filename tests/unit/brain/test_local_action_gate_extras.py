"""Timers, weather, rates, calculator, notes and help phrases (fork addition)."""

from __future__ import annotations

import pytest

from jarvis.brain.local_action_gate import match_local_action
from jarvis.plugins.tool.info_tools import _rank_places, calculate_expression

_LIVE = ("open_app", "set_timer", "get_weather", "get_rates", "calculate", "file_ops", "pc_control")


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("Поставь таймер на 5 минут", "set_timer", {"action": "set", "seconds": 300.0}),
        ("таймер на десять минут", "set_timer", {"action": "set", "seconds": 600.0}),
        ("таймер на 1 час 30 минут", "set_timer", {"action": "set", "seconds": 5400.0}),
        ("таймер на полчаса", "set_timer", {"action": "set", "seconds": 1800.0}),
        ("таймер на двадцать пять минут", "set_timer", {"action": "set", "seconds": 1500.0}),
        ("Напомни через 10 минут позвонить маме", "set_timer",
         {"action": "set", "seconds": 600.0, "label": "позвонить маме"}),
        ("Напомни в 18:30 про встречу", "set_timer", {"action": "set", "at": "18:30", "label": "встречу"}),
        ("Какие у меня таймеры", "set_timer", {"action": "list"}),
        ("Отмени таймер", "set_timer", {"action": "cancel"}),
        ("какая погода", "get_weather", {"day": "today"}),
        ("погода завтра", "get_weather", {"day": "tomorrow"}),
        ("посмотри в интернете погоду в Казани", "get_weather", {"day": "today", "city": "казани"}),
        ("курс доллара", "get_rates", {"currency": "доллара"}),
        ("курс валют", "get_rates", {"currency": ""}),
        ("Посчитай 15 процентов от 3400", "calculate", {"expression": "15 процентов от 3400"}),
        ("открой последний скачанный файл", "file_ops", {"action": "latest", "path": "загрузки", "open": True}),
        ("что ты умеешь", "pc_control", {"action": "help"}),
    ],
)
def test_extra_phrases_route_to_tools(text: str, tool: str, args: dict) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].name == tool
    assert dict(plan.tool_calls[0].args) == args


def test_note_phrase_appends_with_original_casing() -> None:
    plan = match_local_action("Запиши заметку Позвонить в банк", live_tool_names=_LIVE)

    assert plan is not None
    call = plan.tool_calls[0]
    assert call.name == "file_ops"
    assert call.args["action"] == "write" and call.args["append"] is True
    assert call.args["text"].rstrip("\n").endswith("Позвонить в банк")


@pytest.mark.parametrize(
    "text",
    [
        "расскажи про погоду в средние века",
        "курс лечения",
        "таймер",
        "напомни мне позже",
        "сколько будет",
        "запиши",
        "что ты думаешь о погоде",
    ],
)
def test_extra_patterns_ignore_ordinary_talk(text: str) -> None:
    assert match_local_action(text, live_tool_names=_LIVE) is None


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("15 процентов от 3400", "= 510"),
        ("12 умножить на 7", "= 84"),
        ("(2+3)*4", "= 20"),
        ("100 / 8", "= 12,5"),
        ("3 400 плюс 600", "= 4000"),
    ],
)
def test_calculator(expr: str, expected: str) -> None:
    assert calculate_expression(expr).endswith(expected)


@pytest.mark.parametrize("expr", ["__import__('os')", "2 ** 9999", "abs(3)", ""])
def test_calculator_rejects_unsafe_input(expr: str) -> None:
    with pytest.raises((ValueError, SyntaxError)):
        calculate_expression(expr)


def test_calculator_division_by_zero() -> None:
    with pytest.raises(ZeroDivisionError):
        calculate_expression("10 / 0")


def test_city_ranking_prefers_the_inflected_stem_over_lookalikes() -> None:
    places = [
        {"name": "Казахстан", "population": 19_000_000},
        {"name": "Казань", "population": 1_300_000},
    ]

    assert _rank_places("казани", places)[0]["name"] == "Казань"
    assert [p["name"] for p in _rank_places("казани", places)] == ["Казань"]
    assert _rank_places("сочи", [{"name": "Сочимилько", "population": 5}, {"name": "Сочи", "population": 400_000}])[0]["name"] == "Сочи"
