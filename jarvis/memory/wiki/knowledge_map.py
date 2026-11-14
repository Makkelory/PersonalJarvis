"""knowledge_map: what Jarvis CAN do, written into the Obsidian vault as linked notes.

Fork addition (2026-11-14). The wiki extractor learns *facts about the user*;
this module documents the assistant itself so Obsidian's graph shows the whole
picture: every tool, every program Jarvis knows how to start (with its
executable path and the Russian names it answers to), and how they connect.

Output (always regenerated, safe to rerun) under ``<vault>/jarvis-knowledge/``:

* ``Умения.md``  - hub linking every tool note
* ``Умения/<tool>.md`` - description, actions, related programs
* ``Программы.md`` - hub linking every program note
* ``Программы/<name>.md`` - kind, launch path, spoken aliases, tools that drive it
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FOLDER = "jarvis-knowledge"
#: tool -> program notes it drives (graph edges between the two hubs).
_TOOL_PROGRAMS = {
    "spotify_control": ["Spotify"], "steam_control": ["Steam"], "happ_vpn": ["Happ"],
    "open_app": [], "close_app": [], "window_control": [], "app_catalog": [], "browser_control": [],
    "memory_notes": [], "scenes": ["Spotify", "Steam"],
}
_BAD = re.compile(r'[\\/:*?"<>|#^\[\]]')


def _safe(name: str) -> str:
    return _BAD.sub(" ", name).strip()[:80] or "unnamed"


def _tool_classes() -> dict[str, Any]:
    """Instantiate the local PC tools (no bus needed) for their metadata."""
    from jarvis.plugins.tool.app_catalog import AppCatalogTool
    from jarvis.plugins.tool.file_ops import FileOpsTool
    from jarvis.plugins.tool.happ_vpn import HappVpnTool
    from jarvis.plugins.tool.info_tools import CalculateTool, GetRatesTool, GetWeatherTool
    from jarvis.plugins.tool.open_app import OpenAppTool
    from jarvis.plugins.tool.pc_control import NetworkStatusTool, PcControlTool
    from jarvis.plugins.tool.reminders import SetTimerTool
    from jarvis.plugins.tool.spotify_control import SpotifyControlTool
    from jarvis.plugins.tool.steam_control import SteamControlTool
    from jarvis.plugins.tool.system_apps import CloseAppTool, ListAppsTool
    from jarvis.plugins.tool.system_control import SetVolumeTool, SystemStatusTool
    from jarvis.plugins.tool.window_control import WindowControlTool
    from jarvis.plugins.tool.browser_control import BrowserControlTool
    from jarvis.plugins.tool.memory_notes import MemoryNotesTool
    from jarvis.plugins.tool.scenes import ScenesTool

    tools = [OpenAppTool, CloseAppTool, ListAppsTool, SetVolumeTool, SystemStatusTool, FileOpsTool, PcControlTool,
             NetworkStatusTool, SetTimerTool, GetWeatherTool, GetRatesTool, CalculateTool, SpotifyControlTool,
             SteamControlTool, HappVpnTool, WindowControlTool, AppCatalogTool, BrowserControlTool, MemoryNotesTool,
             ScenesTool]
    return {cls.name: cls for cls in tools}


def _actions(cls: Any) -> list[str]:
    schema = getattr(cls, "schema", None) or getattr(cls, "parameters_schema", None) or {}
    enum = (schema.get("properties", {}).get("action", {}) or {}).get("enum")
    return list(enum or [])


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8")


def export(vault_root: Path | str = Path("wiki/obsidian-vault")) -> dict[str, int]:
    """Rewrite the generated notes. Returns counts of tools and programs written."""
    from jarvis.plugins.tool import app_catalog

    root = Path(vault_root) / _FOLDER
    entries = [e for e in app_catalog._load() if e["kind"] in {"lnk", "uwp", "exe"}]  # noqa: SLF001
    aliases: dict[str, list[str]] = {}
    for spoken, canon in app_catalog.RU_ALIASES.items():
        aliases.setdefault(canon.lower(), []).append(spoken)

    programs: dict[str, dict[str, Any]] = {}
    for e in entries:
        programs.setdefault(_safe(e["name"]), e)

    tool_for_program: dict[str, list[str]] = {}
    for tool, names in _TOOL_PROGRAMS.items():
        for n in names:
            tool_for_program.setdefault(n, []).append(tool)

    tools = _tool_classes()
    for name, cls in tools.items():
        related = [p for p in _TOOL_PROGRAMS.get(name, []) if p in programs]
        lines = ["---", "type: jarvis-tool", f"tool: {name}", "generated: true", "---", "", f"# {name}", "",
                 str(getattr(cls, "description", "")).strip(), ""]
        acts = _actions(cls)
        if acts:
            lines += ["## Действия", ""] + [f"- `{a}`" for a in acts] + [""]
        if related:
            lines += ["## Управляет программами", ""] + [f"- [[{_FOLDER}/Программы/{p}|{p}]]" for p in related] + [""]
        lines += ["Назад: [[" + _FOLDER + "/Умения|Умения]]", ""]
        _write(root / "Умения" / f"{_safe(name)}.md", "\n".join(lines))
    hub = ["---", "type: jarvis-hub", "generated: true", "---", "", "# Умения Jarvis", "",
           "Всё, что Jarvis умеет делать на этом компьютере. Каждая заметка — один инструмент.", ""]
    hub += [f"- [[{_FOLDER}/Умения/{_safe(n)}|{n}]]" for n in sorted(tools)]
    hub += ["", "Связанные: [[" + _FOLDER + "/Программы|Программы]], [[memory]], [[entities/user|Пользователь]]", ""]
    _write(root / "Умения.md", "\n".join(hub))

    for name, e in programs.items():
        canon = {name.lower(), e["key"]}
        spoken = sorted({s for c in canon for s in aliases.get(c, [])})
        where = e.get("exe") or e.get("target")
        lines = ["---", "type: jarvis-program", f"kind: {e['kind']}", "generated: true", "---", "", f"# {name}", "",
                 f"- Запуск: `{where}`"]
        if spoken:
            lines.append("- Называется голосом: " + ", ".join(spoken))
        lines.append("")
        drivers = tool_for_program.get(name, [])
        if drivers:
            lines += ["## Инструменты", ""] + [f"- [[{_FOLDER}/Умения/{t}|{t}]]" for t in drivers] + [""]
        lines += ["Назад: [[" + _FOLDER + "/Программы|Программы]]", ""]
        _write(root / "Программы" / f"{name}.md", "\n".join(lines))
    phub = ["---", "type: jarvis-hub", "generated: true", "---", "", "# Программы на этом ПК", "",
            "Программы, которые Jarvis умеет запускать, и пути к их исполняемым файлам.", ""]
    phub += [f"- [[{_FOLDER}/Программы/{n}|{n}]]" for n in sorted(programs, key=str.lower)]
    phub += ["", "Связанные: [[" + _FOLDER + "/Умения|Умения]]", ""]
    _write(root / "Программы.md", "\n".join(phub))
    logger.info("knowledge_map: %d tools, %d programs written to %s", len(tools), len(programs), root)
    return {"tools": len(tools), "programs": len(programs)}


def export_in_background(vault_root: Path | str = Path("wiki/obsidian-vault")) -> None:
    """Write the notes on a daemon thread; never raises, never blocks startup."""
    import threading

    def _run() -> None:
        try:
            export(vault_root)
        except Exception:  # noqa: BLE001 - documentation must not break the assistant
            logger.warning("knowledge_map: export failed", exc_info=True)

    threading.Thread(target=_run, name="knowledge-map-export", daemon=True).start()
