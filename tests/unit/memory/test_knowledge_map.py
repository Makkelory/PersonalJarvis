"""The generated Obsidian map of Jarvis' tools and programs (fork addition)."""
from __future__ import annotations

from jarvis.memory.wiki import knowledge_map
from jarvis.plugins.tool import app_catalog


def test_export_links_tools_and_programs(tmp_path, monkeypatch) -> None:
    entries = [{"name": "Spotify", "key": "spotify", "kind": "lnk", "target": "C:/x/Spotify.lnk", "exe": "C:/s/Spotify.exe"}]
    monkeypatch.setattr(app_catalog, "_entries", entries)

    counts = knowledge_map.export(tmp_path)

    root = tmp_path / "jarvis-knowledge"
    assert counts["programs"] == 1 and counts["tools"] >= 10
    tool_note = (root / "Умения" / "spotify_control.md").read_text(encoding="utf-8")
    assert "[[jarvis-knowledge/Программы/Spotify|Spotify]]" in tool_note
    program_note = (root / "Программы" / "Spotify.md").read_text(encoding="utf-8")
    assert "spotify_control" in program_note and "спотифай" in program_note
