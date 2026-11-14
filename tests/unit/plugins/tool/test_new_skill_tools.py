"""browser sites, scenes and command history (fork addition)."""
from __future__ import annotations

import json

import pytest

from jarvis.memory import command_history
from jarvis.plugins.tool import browser_control, scenes


def test_site_names_and_addresses() -> None:
    assert browser_control.site_url("Ютуб") == "https://www.youtube.com"
    assert browser_control.site_url("github.com") == "https://github.com"
    assert browser_control.site_url("абракадабра") is None


def test_command_history_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(command_history, "_PATH", tmp_path / "h.jsonl")

    command_history.record("открой блокнот", ["open_app"])
    command_history.record("запомни что-то", ["memory_notes"])  # not journaled

    assert "открой блокнот" in command_history.summary(0)
    assert "запомни" not in command_history.summary(0)
    assert "нет" in command_history.summary(1)


@pytest.mark.asyncio
async def test_scene_runs_its_steps_and_rejects_unsafe_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scenes, "_STORE", tmp_path / "scenes.json")
    calls: list[dict] = []

    class Fake:
        async def execute(self, args, ctx):
            calls.append(args)
            from jarvis.core.protocols import ToolResult
            return ToolResult(success=True, output="ok")

    scenes.bind({"spotify_control": Fake(), "set_volume": Fake(), "open_app": Fake()})
    tool = scenes.ScenesTool()

    refused = await tool.execute({"action": "save", "name": "x", "steps": [{"tool": "run_shell", "args": {}}]}, None)
    assert refused.success is False

    saved = await tool.execute({"action": "save", "name": "вечер", "steps": [{"tool": "set_volume", "args": {"action": "set", "level": 30}}]}, None)
    assert saved.success
    ran = await tool.execute({"action": "run", "name": "вечерний режим"}, None)
    assert ran.success and calls == [{"action": "set", "level": 30}]
    assert json.loads((tmp_path / "scenes.json").read_text(encoding="utf-8"))["вечер"]


@pytest.mark.asyncio
async def test_learn_writes_a_linked_knowledge_note(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import memory_notes

    monkeypatch.setattr(memory_notes, "_VAULT", tmp_path)
    (tmp_path / "jarvis-knowledge" / "Умения").mkdir(parents=True)
    (tmp_path / "jarvis-knowledge" / "Умения" / "spotify_control.md").write_text("x", encoding="utf-8")

    result = await memory_notes.MemoryNotesTool().execute(
        {"action": "learn", "topic": "Медиаклавиши", "text": "Играет через медиаклавиши.",
         "source": "https://example.test", "related": ["spotify_control", "Незнакомое"]}, None)

    assert result.success
    note = (tmp_path / "jarvis-knowledge" / "Знания" / "Медиаклавиши.md").read_text(encoding="utf-8")
    assert "[[jarvis-knowledge/Умения/spotify_control|spotify_control]]" in note
    assert "Источник: https://example.test" in note
    assert "Медиаклавиши" in (tmp_path / "jarvis-knowledge" / "Знания.md").read_text(encoding="utf-8")


def test_study_course_runs_block_by_block_and_links_notes(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import study_course as sc

    monkeypatch.chdir(tmp_path)
    tool = sc.StudyCourseTool()

    assert "58" in tool._run({"action": "start", "course": "Computer Science"})
    assert "1.1" in tool._run({"action": "next"})
    short = tool._run({"action": "complete", "block_id": "1.1", "summary": "коротко"})
    assert short.startswith("ERROR:")
    summary = ("Вычисление преобразует информацию по правилам. Бит хранит ноль или единицу, байт состоит из восьми бит. "
               "Любые данные в компьютере кодируются двоичными последовательностями.")
    done = tool._run({"action": "complete", "block_id": "1.1", "summary": summary, "key_terms": ["бит"], "source": "https://example.test"})
    assert "1 из 58" in done and "спроси" in done.lower()
    second = tool._run({"action": "complete", "block_id": "1.2", "summary": summary})
    assert "1.2" in second

    root = tmp_path / "wiki" / "obsidian-vault" / "jarvis-knowledge" / "Знания"
    note2 = next(root.glob("1.2 *.md")).read_text(encoding="utf-8")
    assert "Курс Computer Science" in note2 and "1.1 " in note2
    hub = (root / "Курс Computer Science.md").read_text(encoding="utf-8")
    assert "- [x] 1.1" in hub and "- [x] 1.2" in hub and "- [ ] 1.3" in hub
    assert "2 из 58" in tool._run({"action": "status"})


def test_start_returns_the_first_block_and_complete_arms_the_continue_gate(tmp_path, monkeypatch) -> None:
    from jarvis.brain.local_action_gate import match_local_action
    from jarvis.plugins.tool import study_course as sc

    monkeypatch.chdir(tmp_path)
    tool = sc.StudyCourseTool()

    started = tool._run({"action": "start", "course": "Computer Science"})
    assert "СЛЕДУЮЩИЙ БЛОК 1.1" in started
    assert not sc.awaiting_continue()
    assert match_local_action("дальше") is not None  # no course waiting: it is a media command

    summary = ("Вычисление преобразует информацию по правилам. Бит хранит ноль или единицу, байт состоит из восьми бит. "
               "Любые данные в компьютере кодируются двоичными последовательностями.")
    tool._run({"action": "complete", "block_id": "1.1", "summary": summary})

    assert sc.awaiting_continue()
    for phrase in ("дальше", "продолжай", "Да"):
        plan = match_local_action(phrase)
        assert plan is not None and plan.tool_calls[0].name == "study_course"
        assert plan.tool_calls[0].args == {"action": "study"}
    assert match_local_action("следующий трек") is not None  # explicit music words still work
    tool._run({"action": "next"})
    assert not sc.awaiting_continue()


def test_files_become_courses_and_the_waiting_course_is_picked(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import study_course as sc

    monkeypatch.chdir(tmp_path)
    inbox = tmp_path / "data" / "courses_inbox"
    inbox.mkdir(parents=True)
    body = "Текст про командную строку и файлы. " * 40
    (inbox / "Терминал Windows.txt").write_text("# Основы\n\n" + body + "\n\n# Файлы\n\n" + body * 2, encoding="utf-8")
    (inbox / "заметки.docx").write_bytes(b"not supported")

    assert sc._find_file("терминал").name == "Терминал Windows.txt"
    assert sc._find_file("нет такого") is None
    assert [p.name for p in sc._inbox_files()] == ["Терминал Windows.txt"]

    blob: dict = {}
    key = sc._ingest_file(sc._find_file("терминал"), blob)
    course = sc._load()[key]
    assert key == "file-терминал-windows" and len(course["blocks"]) == 2 and course["source"].endswith(".txt")
    assert course["blocks"][0]["title"] == "Основы"
    texts = __import__("json").loads((tmp_path / "data" / "course_texts" / f"{key}.json").read_text(encoding="utf-8"))
    assert "командную строку" in texts["1.1"]
    hub = tmp_path / "wiki" / "obsidian-vault" / "jarvis-knowledge" / "Знания" / "Курс Терминал Windows.md"
    assert "- [ ] 1.2" in hub.read_text(encoding="utf-8")
    assert sc._ingest_file(sc._find_file("терминал"), sc._load()) == key  # unchanged file: no duplicate

    saved = sc._load()
    saved[key]["awaiting"] = 123.0
    assert sc._pick_key("", saved) == key
    assert sc._pick_key("Computer Science", saved) == "computer-science"


def test_knowledge_collection_puts_the_playbook_first(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import skill_from_knowledge as sk

    vault = tmp_path / "wiki" / "obsidian-vault"
    folder = vault / "jarvis-knowledge" / "Знания"
    folder.mkdir(parents=True)
    monkeypatch.setattr(sk, "_VAULT", vault)
    (folder / "Приёмы Терминал Windows.md").write_text("---\ntype: x\n---\n\nGet-ChildItem — список файлов\nCopy-Item a b", encoding="utf-8")
    (folder / "1.1 Основы терминала Windows.md").write_text(
        "---\ntype: y\n---\n\nКонспект про терминал windows и командлеты.\n\nСвязано: [[x]]\n\nКурс Терминал Windows", encoding="utf-8")
    (folder / "Курс Терминал Windows.md").write_text("hub", encoding="utf-8")
    (folder / "Про кошек.md").write_text("кошки и котики", encoding="utf-8")

    notes = sk._collect("терминал windows", "Терминал Windows")

    assert [p.stem for p, _t in notes] == ["Приёмы Терминал Windows", "1.1 Основы терминала Windows"]
    text = sk._knowledge_text(notes)
    assert "Get-ChildItem" in text and "Связано" not in text and "кошки" not in text
    assert "ТОЧНЫЕ команды" in sk._intent("Терминал", text)


@pytest.mark.asyncio
async def test_make_skill_without_knowledge_is_an_honest_failure(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import skill_from_knowledge as sk

    monkeypatch.setattr(sk, "_VAULT", tmp_path)

    ok, message, _info = await sk.make_skill("несуществующая тема")

    assert ok is False and "не нашёл" in message


@pytest.mark.asyncio
async def test_learn_notes_carry_the_properties_the_obsidian_base_filters_on(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import memory_notes

    monkeypatch.setattr(memory_notes, "_VAULT", tmp_path)

    memory_notes._learn("1.1 Тема", "Текст про тему.", "", [], {"course": "Computer Science", "module": "Основы", "block": "1.1"})

    head = (tmp_path / "jarvis-knowledge" / "Знания" / "1.1 Тема.md").read_text(encoding="utf-8").split("---")[1]
    assert "type: jarvis-knowledge" in head and "course: Computer Science" in head and "block: 1.1" in head


def test_inflected_program_names_resolve() -> None:
    from jarvis.plugins.tool import app_catalog

    assert app_catalog.spoken_to_canonical("блокноте") == "notepad"
    assert app_catalog.spoken_to_canonical("дискордом") == "discord"
    assert app_catalog.spoken_to_canonical("в стиме") != "steam"  # a preposition is cut by the gate, not here
    assert app_catalog.spoken_to_canonical("абракадабра") == "абракадабра"


def test_app_control_refuses_destructive_controls_without_confirmation() -> None:
    from jarvis.plugins.tool import app_control

    text = app_control._do("click", {"program": "что-угодно", "element": "Удалить все данные"})
    assert "опасным" in text
    assert "Укажите программу" in app_control._do("inspect", {})
    assert "Не понял сочетание" in app_control._do("hotkey", {"keys": "ctrl+неведомая"})


def test_study_app_skips_games_installers_and_urls(monkeypatch) -> None:
    from jarvis.plugins.tool import app_catalog, app_study

    def entry(name, exe, kind="lnk"):
        return {"name": name, "key": name.lower(), "kind": kind, "target": "x.lnk", "exe": exe}

    monkeypatch.setattr(app_catalog, "_entries", [
        entry("Discord", "C:/Users/a/Discord.exe"),
        entry("Dota 2", "C:/Steam/steamapps/common/dota/dota2.exe"),
        entry("Uninstall Foo", "C:/Foo/unins000.exe"),
        entry("Steam Game Link", ""),
        entry("Калькулятор", "", kind="uwp"),
        entry("Notepad++", "C:/npp/notepad++.exe", kind="scan"),
    ])

    assert [e["name"] for e in app_study._candidates()] == ["Discord", "Калькулятор"]


def test_continue_goes_to_the_study_that_asked_last(tmp_path, monkeypatch) -> None:
    import json
    import time

    from jarvis.plugins.tool import study_course as sc

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    assert sc.awaiting_target() is None
    (tmp_path / "data" / "courses.json").write_text(json.dumps(
        {"x": {"state": "active", "awaiting": time.time() - 100, "blocks": [], "done": {}}}), encoding="utf-8")
    assert sc.awaiting_target()[0] == "study_course"
    (tmp_path / "data" / "app_knowledge.json").write_text(json.dumps({"apps": {}, "awaiting": time.time()}), encoding="utf-8")
    assert sc.awaiting_target() == ("study_app", {"action": "study_next"})


def test_windows_course_syllabus_and_names() -> None:
    from jarvis.plugins.tool import study_course as sc

    modules = sc.SYLLABI["windows"]
    assert len(modules) >= 15 and sum(len(blocks) for _m, blocks in modules) >= 100
    titles = [m for m, _b in modules]
    assert "Командная строка cmd" in titles and "PowerShell" in titles and "Сеть" in titles
    assert "windows" in sc.PRACTICAL
    for spoken in ("Windows", "виндовс", "powershell", "командная строка", "cmd"):
        assert sc._slug(spoken) == "windows"
    assert sc._slug("Computer Science") == "computer-science"


def test_system_tools_are_catalog_programs_with_launch_commands() -> None:
    from jarvis.plugins.tool import windows_system as ws

    names = {t[0] for t in ws.SYSTEM_TOOLS}
    assert {"Командная строка", "Windows PowerShell", "Диспетчер задач", "Редактор реестра", "Службы"} <= names
    aliases = ws.spoken_aliases()
    assert aliases["павершелл"] == "windows powershell" and aliases["диспетчер задач"] == "диспетчер задач"
    from jarvis.plugins.tool import app_catalog

    assert app_catalog.RU_ALIASES["просмотр событий"] == "просмотр событий"
    assert "powershell" not in {k for k in app_catalog.RU_ALIASES if k == "powershell"}  # English names are not remapped
    assert all(t[4] for t in ws.SYSTEM_TOOLS)  # every tool documents how to start it from cmd


def test_msc_and_cpl_targets_are_opened_by_the_shell(monkeypatch) -> None:
    from jarvis.plugins.tool import app_catalog, app_resolver

    monkeypatch.setattr(app_resolver, "detect_platform", lambda: "win32")

    assert app_resolver.resolve_app_launch_target("службы") == app_resolver.LaunchTarget("startfile", "services.msc")
    entry = {"name": "Службы", "key": "службы", "kind": "sys", "target": "C:/Windows/System32/services.msc", "exe": "C:/Windows/System32/services.msc"}
    monkeypatch.setattr(app_catalog, "_entries", [entry])
    assert app_catalog.launch_target_for("службы").kind == "startfile"


def test_skill_collection_can_be_scoped_to_one_module(tmp_path, monkeypatch) -> None:
    from jarvis.plugins.tool import skill_from_knowledge as sk

    vault = tmp_path / "wiki" / "obsidian-vault"
    folder = vault / "jarvis-knowledge" / "Знания"
    folder.mkdir(parents=True)
    monkeypatch.setattr(sk, "_VAULT", vault)
    (folder / "2.1 Диски.md").write_text("---\ntype: jarvis-knowledge\ncourse: Windows\nmodule: Диски\n---\n\nDiskpart и Get-Disk про диски windows", encoding="utf-8")
    (folder / "3.1 Сеть.md").write_text("---\ntype: jarvis-knowledge\ncourse: Windows\nmodule: Сеть\n---\n\nipconfig и ping про сеть windows", encoding="utf-8")
    (folder / "Приёмы Windows.md").write_text("---\ntype: jarvis-playbook\ncourse: Windows\n---\n\nGet-Disk windows", encoding="utf-8")

    notes = sk._collect("Windows: Диски", "Windows", "Диски")

    assert [p.stem for p, _t in notes] == ["Приёмы Windows", "2.1 Диски"]


def test_page_text_keeps_the_commands_in_code_blocks() -> None:
    from jarvis.plugins.tool.study_course import _ParagraphText

    parser = _ParagraphText()
    parser.feed("<p>Список дисков:</p><pre><code>Get-Disk\nGet-Partition -DiskNumber 0</code></pre><script>x()</script>")
    text = "".join(parser.parts)

    assert "Get-Disk\nGet-Partition -DiskNumber 0" in text and "x()" not in text
