"""Spotify, Steam and Happ phrases and their pure helpers (fork addition)."""

from __future__ import annotations

import pytest

from jarvis.brain.local_action_gate import match_local_action

_LIVE = ("open_app", "spotify_control", "steam_control", "happ_vpn", "pc_control", "close_app")


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("следующий трек", "spotify_control", {"action": "next"}),
        ("предыдущий трек", "spotify_control", {"action": "previous"}),
        ("пауза", "spotify_control", {"action": "play_pause"}),
        ("что сейчас играет", "spotify_control", {"action": "now_playing"}),
        ("включи случайный трек", "spotify_control", {"action": "random_track"}),
        ("включи перемешивание", "spotify_control", {"action": "shuffle_on"}),
        ("выключи перемешивание", "spotify_control", {"action": "shuffle_off"}),
        ("включи повтор", "spotify_control", {"action": "repeat_on"}),
        ("выключи повтор", "spotify_control", {"action": "repeat_off"}),
        ("добавь трек в любимое", "spotify_control", {"action": "like"}),
        ("убери трек из любимого", "spotify_control", {"action": "unlike"}),
        ("включи радио по треку", "spotify_control", {"action": "radio"}),
        ("установи Team Fortress 2 в стиме на диск E", "steam_control",
         {"action": "install", "game": "Team Fortress 2", "drive": "E"}),
        ("установи Bodycam в стиме", "steam_control", {"action": "install", "game": "Bodycam"}),
        ("запусти Counter-Strike 2 в стиме", "steam_control", {"action": "run", "game": "Counter-Strike 2"}),
        ("открой загрузки в стиме", "steam_control", {"action": "open_downloads"}),
        ("останови загрузки в стиме", "steam_control", {"action": "pause_downloads"}),
        ("возобнови загрузки в стиме", "steam_control", {"action": "resume_downloads"}),
        ("какие библиотеки в стиме", "steam_control", {"action": "libraries"}),
        ("стань невидимкой в стиме", "steam_control", {"action": "friend_status", "state": "невидимка"}),
        ("поставь статус в стиме нет на месте", "steam_control", {"action": "friend_status", "state": "нет на месте"}),
        ("смени статус в стиме на в сети", "steam_control", {"action": "friend_status", "state": "в сети"}),
        ("запусти пинг в хапп", "happ_vpn", {"action": "ping"}),
        ("подключись к самому быстрому серверу", "happ_vpn", {"action": "connect_fastest"}),
        ("подключись к серверу с минимальной задержкой", "happ_vpn", {"action": "connect_fastest"}),
        ("подключись к серверу Чехия", "happ_vpn", {"action": "connect", "server": "Чехия"}),
        ("включён ли впн", "happ_vpn", {"action": "status"}),
    ],
)
def test_app_phrases_route_to_tools(text: str, tool: str, args: dict) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].name == tool
    assert dict(plan.tool_calls[0].args) == args


@pytest.mark.parametrize(
    "text",
    [
        "расскажи про радио",
        "включи радио",
        "установи программу",
        "скачай фильм",
        "запусти игру",
        "подключись к вайфаю",
        "что играет в фильме Матрица",
        "поставь статус",
        "пауза в разговоре",
        "открой стим",
    ],
)
def test_app_patterns_ignore_ordinary_talk(text: str) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is None or plan.tool_calls[0].name in {"open_app"}


def test_install_drive_letter_is_not_taken_from_the_word_steam() -> None:
    plan = match_local_action("установи Bodycam в стиме", live_tool_names=_LIVE)

    assert plan is not None
    assert "drive" not in plan.tool_calls[0].args


@pytest.mark.parametrize(
    ("text", "expected"),
    [("211ms", 211), ("19Bms", 198), ("3SBms", 358), ("1S49ms", 1549), ("193 ms", 193), ("xms", None), ("", None)],
)
def test_happ_latency_parsing_tolerates_ocr_slips(text: str, expected: int | None) -> None:
    from jarvis.plugins.tool.happ_vpn import parse_ms

    assert parse_ms(text) == expected


def test_steam_selected_row_is_recognised_by_its_blue_background() -> None:
    from PIL import Image

    from jarvis.plugins.tool.steam_control import _row_selected

    blue = Image.new("RGB", (600, 100), (26, 159, 255))
    grey = Image.new("RGB", (600, 100), (60, 64, 72))

    assert _row_selected(blue, 100, 50) is True
    assert _row_selected(grey, 100, 50) is False


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("Статус друзей в стиме в сети", "в сети"),
        ("Статус стим В сети", "в сети"),
        ("Статус друзей в стиме Нет на месте", "нет на месте"),
        ("Поставь в стиме невидимка", "невидимка"),
        ("Статус друзей в стиме неведимка", "невидимка"),
        ("Статус в стиме не в сети", "не в сети"),
        ("Смени статус стим на не беспокоить", "не беспокоить"),
    ],
)
def test_free_word_steam_status(text: str, state: str) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is not None
    assert plan.tool_calls[0].name == "steam_control"
    assert dict(plan.tool_calls[0].args) == {"action": "friend_status", "state": state}


@pytest.mark.parametrize(
    "text",
    ["почему в стиме не в сети мои друзья", "как поставить статус в стиме?", "в стиме новая игра в сети"],
)
def test_free_word_steam_status_ignores_questions_and_other_talk(text: str) -> None:
    plan = match_local_action(text, live_tool_names=_LIVE)

    assert plan is None or plan.tool_calls[0].name != "steam_control"


@pytest.mark.parametrize("phrase", ["открой стим", "Джарвис, запусти стим", "включи steam", "открой игру в стиме"])
def test_opening_steam_without_a_game_just_opens_the_client(phrase: str) -> None:
    plan = match_local_action(phrase)

    assert plan is not None
    assert [(c.name, c.args) for c in plan.tool_calls] == [("open_app", {"app_name": "steam"})]


@pytest.mark.parametrize(
    "phrase", ["открой игру Dota 2 в стиме", "открой в стиме Dota 2", "открой стим игру Dota 2", "запусти Dota 2 в стиме"],
)
def test_a_named_steam_game_is_launched(phrase: str) -> None:
    plan = match_local_action(phrase)

    assert plan is not None
    call = plan.tool_calls[0]
    assert call.name == "steam_control"
    assert call.args == {"action": "run", "game": "Dota 2"}
