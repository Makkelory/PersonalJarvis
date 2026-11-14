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
