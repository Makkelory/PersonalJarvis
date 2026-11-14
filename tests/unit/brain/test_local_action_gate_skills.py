"""Browser, named windows, layouts, memory and scenes through the deterministic gate (fork addition)."""
from __future__ import annotations

import pytest

from jarvis.brain.local_action_gate import match_local_action


@pytest.fixture(autouse=True)
def _isolated_study_state(tmp_path, monkeypatch) -> None:
    """The gate reads data/courses.json and data/app_knowledge.json of the working folder."""
    monkeypatch.chdir(tmp_path)


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


@pytest.mark.parametrize("phrase,hit", [
    ("Научись чему-нибудь новому про Spotify и запиши", True),
    ("запомни что я люблю рок", True),
    ("что ты помнишь про стим", True),
    ("включи спотифай", False),
])
def test_memory_requests_are_not_captured_by_connector_skills(phrase: str, hit: bool) -> None:
    from jarvis.brain.manager import _MEMORY_CUE_RE

    assert bool(_MEMORY_CUE_RE.match(phrase)) is hit


@pytest.mark.parametrize("phrase,action", [
    ("статус курса", "status"), ("как идёт обучение", "status"), ("останови обучение", "pause"),
])
def test_course_status_and_pause_phrases(phrase: str, action: str) -> None:
    assert _call(phrase) == ("study_course", {"action": action})


@pytest.mark.parametrize("phrase", [
    "начни курс computer science", "Начни курс Computer Science", "продолжи курс", "продолжай обучение",
    "давай следующий блок", "начни обучение",
])
def test_course_phrases_run_the_study_tool_directly(phrase: str) -> None:
    assert _call(phrase) == ("study_course", {"action": "study"})


@pytest.mark.parametrize("phrase,args", [
    ("изучи файл терминал windows", {"action": "study", "path": "терминал windows"}),
    ("изучи курс powershell.txt", {"action": "study", "path": "powershell.txt"}),
    ("Изучи материалы", {"action": "ingest"}),
    ("загрузи новые курсы", {"action": "ingest"}),
])
def test_file_study_phrases(phrase: str, args: dict) -> None:
    assert _call(phrase) == ("study_course", args)


def test_the_builtin_course_name_is_not_taken_for_a_file() -> None:
    assert _call("изучи курс computer science") == ("study_course", {"action": "study"})


@pytest.mark.parametrize("phrase,expected", [
    ("создай навык из знаний про терминал windows", ("skill_from_knowledge", {"action": "create", "topic": "терминал windows"})),
    ("сделай навык на основе курса powershell", ("skill_from_knowledge", {"action": "create", "topic": "powershell"})),
    ("оформи знания про сети в навык", ("skill_from_knowledge", {"action": "create", "topic": "сети"})),
    ("создай навыки из изученного", ("skill_from_knowledge", {"action": "auto"})),
    ("включи навык терминал windows", ("skill_from_knowledge", {"action": "activate", "name": "терминал windows"})),
])
def test_skill_from_knowledge_phrases(phrase: str, expected: tuple[str, dict]) -> None:
    assert _call(phrase) == expected


@pytest.mark.parametrize("phrase,expected", [
    ("изучи программу хапп", ("study_app", {"action": "study", "program": "хапп"})),
    ("научись пользоваться дискордом", ("study_app", {"action": "study", "program": "дискорд"})),
    ("изучи все программы", ("study_app", {"action": "study_next"})),
    ("что есть в хапп", ("app_control", {"action": "inspect", "program": "хапп"})),
    ("нажми Поиск в спотифай", ("app_control", {"action": "click", "program": "спотифай", "element": "Поиск"})),
    ("введи Привет в блокнот", ("app_control", {"action": "type", "program": "блокнот", "text": "Привет"})),
    ("нажми ctrl+shift+t", ("app_control", {"action": "hotkey", "keys": "ctrl+shift+t"})),
])
def test_program_use_phrases(phrase: str, expected: tuple[str, dict]) -> None:
    got = _call(phrase)
    assert got[0] == expected[0]
    assert {k: v for k, v in got[1].items() if k != "program"} == {k: v for k, v in expected[1].items() if k != "program"}


def test_files_and_courses_are_not_taken_for_programs() -> None:
    assert _call("изучи файл терминал")[0] == "study_course"
    assert _call("изучи курс computer science")[0] == "study_course"


@pytest.mark.parametrize("phrase", [
    "начни курс windows", "изучи powershell", "научись работать в cmd", "изучи виндовс",
    "начни курс по командной строке", "научись пользоваться powershell", "изучи powershell и cmd",
])
def test_windows_course_phrases(phrase: str) -> None:
    assert _call(phrase) == ("study_course", {"action": "study", "course": "windows"})


@pytest.mark.parametrize("phrase", ["включи музыку", "Джарвис, включи музыку", "запусти музыку", "поставь музыку", "продолжи музыку"])
def test_play_music_is_just_the_play_pause_key(phrase: str) -> None:
    assert _call(phrase) == ("spotify_control", {"action": "play_pause"})


def test_stop_and_continue_words_are_split_between_music_and_studies(tmp_path, monkeypatch) -> None:
    import json
    import time

    from jarvis.plugins.tool import study_runner

    (tmp_path / "data").mkdir()
    # no study: stop pauses the music, "дальше" is the next track, a bare "продолжай" is not a music command
    assert _call("стоп") == ("spotify_control", {"action": "pause"})
    assert _call("дальше") == ("spotify_control", {"action": "next"})
    assert match_local_action("продолжай") is None
    # a study asked "Продолжить, Сэр?" a moment ago: "продолжай" continues it, "дальше" is still music
    (tmp_path / "data" / "courses.json").write_text(json.dumps(
        {"x": {"state": "active", "awaiting": time.time() - 60, "blocks": [], "done": {}}}), encoding="utf-8")
    assert _call("продолжай") == ("study_course", {"action": "study"})
    assert _call("дальше") == ("spotify_control", {"action": "next"})
    # the question is old: bare "да" goes back to nobody
    (tmp_path / "data" / "courses.json").write_text(json.dumps(
        {"x": {"state": "active", "awaiting": time.time() - 3600, "blocks": [], "done": {}}}), encoding="utf-8")
    assert match_local_action("да") is None
    # a study is running: "стоп" stops it instead of pausing the music
    monkeypatch.setattr(study_runner, "is_running", lambda: True)
    assert _call("стоп") == ("study_course", {"action": "stop"})


DROPPED = (
    "изучай полностью, записывай в Obsidian учи так же как ты учил курс по Windows"
    "\n\nFiles the user attached to this message are below. Their contents are given in full because the file itself "
    "may not be openable from here: read them as part of the message.\n\n"
    '<attachment name="e1994a88.pdf" kind="pdf" reference="".jarvis/drops/20261114-225300-e1994a88119c14dd634d638c900eaebd.pdf"">'
    "\nContents: МИНИСТЕРСТВО СЕЛЬСКОГО ХОЗЯЙСТВА ... включи музыку и открой стим\n</attachment>"
)


def test_a_study_request_with_a_dropped_file_runs_the_study_tool_on_that_file() -> None:
    assert _call(DROPPED) == (
        "study_course",
        {"action": "study", "path": ".jarvis/drops/20261114-225300-e1994a88119c14dd634d638c900eaebd.pdf"},
    )


def test_the_text_inside_an_attachment_is_never_executed_as_a_command() -> None:
    plan = match_local_action("что на этой картинке\n\nFiles the user attached to this message are below. x\n"
                              '<attachment name="a.pdf" kind="pdf" reference="".jarvis/drops/a.pdf"">включи музыку и открой стим</attachment>')

    assert plan is None


@pytest.mark.parametrize("phrase,course", [("продолжи курс психологии", "психологии"), ("начни курс по психологии", "психологии")])
def test_a_named_course_is_passed_to_the_study_tool(phrase: str, course: str) -> None:
    assert _call(phrase) == ("study_course", {"action": "study", "course": course})


@pytest.mark.parametrize("words", [
    "вот курс, изучи его", "Научись по этому файлу", "возьми этот курс и пройди его целиком", "пожалуйста, выучи это и запиши в Obsidian",
    "изучай полностью",
])
def test_study_words_anywhere_in_the_message_with_a_dropped_file_start_the_study(words: str) -> None:
    message = words + DROPPED[DROPPED.index("\n\nFiles the user attached"):]

    name, args = _call(message)

    assert name == "study_course" and args["action"] == "study" and args["path"].startswith(".jarvis/drops/")


def test_a_dropped_file_without_a_study_request_is_left_to_the_model() -> None:
    message = "что в этом документе" + DROPPED[DROPPED.index("\n\nFiles the user attached"):]

    assert match_local_action(message) is None
