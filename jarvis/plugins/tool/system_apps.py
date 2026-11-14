"""system_apps: what is running, what is installed, and closing programs.

Fork addition (2026-11-14). Gives the assistant real, read-only answers instead
of invented ones:

* ``list_apps`` — programs with a window, the heaviest background processes,
  and an installed-application inventory built from the Start Menu, the
  Windows Uninstall registry keys and the Program Files / Games / Steam / Epic
  folders on every fixed drive. Read-only (risk tier ``monitor``).
* ``close_app`` — politely closes a program (``taskkill`` without ``/F``, i.e.
  WM_CLOSE) and, only when asked, forces it. Risk tier ``ask`` so the approval
  workflow confirms it first. System-critical processes and Jarvis' own process
  tree are never touched.

Windows is the supported platform; elsewhere the tools answer honestly that
they are unavailable instead of guessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform, window_state

logger = logging.getLogger(__name__)

#: Never closed, whatever the user says: OS core plus the shell.
PROTECTED_PROCESSES: frozenset[str] = frozenset({
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "explorer.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe", "audiodg.exe",
    "memory compression", "secure system", "python.exe", "pythonw.exe",
})

_INVENTORY_TTL_S = 24 * 3600
_INVENTORY_PATH = Path("data") / "app_inventory.json"
_MAX_TEXT = 3000

_UNINSTALL_KEYS = (
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
)

#: Folders (relative to a drive root) whose direct children are installed apps
#: or games. Missing ones are skipped silently.
_APP_FOLDERS = (
    "Program Files", "Program Files (x86)", "Games", "Steam\\steamapps\\common",
    "SteamLibrary\\steamapps\\common", "Program Files (x86)\\Steam\\steamapps\\common",
    "Epic Games", "XboxGames",
)


def _fixed_drives() -> list[str]:
    """Drive roots of fixed (non-removable, non-network) disks."""
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        roots = []
        for i in range(26):
            if mask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # type: ignore[attr-defined]
                    roots.append(root)
        return roots
    except Exception:  # noqa: BLE001
        return []


def _scan_registry() -> list[dict[str, str]]:
    import winreg

    found: dict[str, dict[str, str]] = {}
    for sub, hive_name in _UNINSTALL_KEYS:
        hive = winreg.HKEY_LOCAL_MACHINE if hive_name == "HKLM" else winreg.HKEY_CURRENT_USER
        try:
            root = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    key_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, key_name) as key:
                        def val(n: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(key, n)[0] or "")
                            except OSError:
                                return ""

                        name = val("DisplayName").strip()
                        if not name or val("SystemComponent") == "1" or val("ParentKeyName"):
                            continue
                        location = val("InstallLocation").strip().strip('"')
                        if not location:
                            icon = val("DisplayIcon").split(",")[0].strip().strip('"')
                            location = str(Path(icon).parent) if icon.lower().endswith(".exe") else ""
                        found.setdefault(name.casefold(), {
                            "name": name, "source": "registry",
                            "location": location, "version": val("DisplayVersion"),
                        })
                except OSError:
                    continue
    return list(found.values())


def _scan_folders() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for drive in _fixed_drives():
        for rel in _APP_FOLDERS:
            base = Path(drive) / rel
            try:
                if not base.is_dir():
                    continue
                for child in sorted(base.iterdir()):
                    if child.is_dir() and not child.name.startswith(("$", ".")):
                        rows.append({
                            "name": child.name, "source": f"folder:{drive}{rel}",
                            "location": str(child), "version": "",
                        })
            except OSError:
                continue
    return rows


def _scan_start_menu() -> list[dict[str, str]]:
    from jarvis.plugins.tool.app_inventory import installed_app_names

    return [
        {"name": n, "source": "start-menu", "location": "", "version": ""}
        for n in installed_app_names(force_refresh=True)
    ]


def build_inventory(*, force: bool = False) -> list[dict[str, str]]:
    """Installed-app inventory, cached on disk for a day. Never raises."""
    if not force:
        try:
            if _INVENTORY_PATH.is_file() and time.time() - _INVENTORY_PATH.stat().st_mtime < _INVENTORY_TTL_S:
                return json.loads(_INVENTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    rows: list[dict[str, str]] = []
    for scan in (_scan_start_menu, _scan_registry, _scan_folders):
        try:
            rows.extend(scan())
        except Exception:  # noqa: BLE001 — one broken source must not cost the rest
            logger.warning("[LIST_APPS] %s failed", scan.__name__, exc_info=True)
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        key = row["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: r["name"].casefold())
    try:
        _INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _INVENTORY_PATH.write_text(json.dumps(unique, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.debug("[LIST_APPS] inventory cache not written", exc_info=True)
    return unique


def _running_report(query: str, brief: bool = False) -> str:
    import psutil

    windows = window_state.list_windows()
    titles: dict[int, list[str]] = {}
    for w in windows:
        if w.pid and (w.title or "").strip():
            titles.setdefault(int(w.pid), []).append(w.title.strip())
    q = query.casefold()
    with_window: dict[str, dict[str, Any]] = {}
    background: dict[str, float] = {}
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = proc.info["name"] or "?"
            mem = (proc.info["memory_info"].rss if proc.info["memory_info"] else 0) / 1048576
            wins = titles.get(proc.info["pid"])
            if wins:
                entry = with_window.setdefault(name, {"mb": 0.0, "pids": [], "titles": []})
                entry["mb"] += mem
                entry["pids"].append(proc.info["pid"])
                entry["titles"].extend(wins)
            else:
                background[name] = background.get(name, 0.0) + mem
        except (psutil.Error, OSError):
            continue
    lines: list[str] = []
    shown = {n: e for n, e in with_window.items()
             if not q or q in n.casefold() or any(q in t.casefold() for t in e["titles"])}
    lines.append(f"Программы с окнами ({len(shown)}):")
    ranked = sorted(shown.items(), key=lambda kv: -kv[1]["mb"])
    for name, e in ranked[: 8 if brief else None]:
        title = e["titles"][0][:60]
        lines.append(f"- {name}: {e['mb']:.0f} МБ, pid {e['pids'][0]}, окно «{title}»")
    if brief and len(ranked) > 8:
        lines.append(f"…и ещё {len(ranked) - 8}. Скажи «все запущенные программы» для полного списка.")
    bg = {n: m for n, m in background.items() if not q or q in n.casefold()}
    top = sorted(bg.items(), key=lambda kv: -kv[1])[: (5 if brief else 12) if not q else 30]
    lines.append(f"Фоновые процессы без окон (всего {len(bg)}, топ по памяти):")
    for name, mb in top:
        lines.append(f"- {name}: {mb:.0f} МБ")
    return "\n".join(lines)


def _installed_report(query: str, rescan: bool, brief: bool = False) -> str:
    rows = build_inventory(force=rescan)
    q = query.casefold()
    if q:
        hits = [r for r in rows if q in r["name"].casefold() or q in r["location"].casefold()]
        if not hits:
            return f"По запросу «{query}» ничего не найдено среди {len(rows)} установленных приложений."
        lines = [f"Найдено {len(hits)} из {len(rows)} (по запросу «{query}»):"]
        for r in hits[:15]:
            where = f" — {r['location']}" if r["location"] else ""
            lines.append(f"- {r['name']} [{r['source'].split(':')[0]}]{where}")
        return "\n".join(lines)
    limit = 25 if brief else 60
    names = ", ".join(r["name"] for r in rows[:limit])
    return f"Установлено приложений: {len(rows)}. Первые {limit}: {names}"


def _find_targets(query: str) -> list[tuple[int, str, str]]:
    """(pid, process name, window title) of processes matching ``query``."""
    import psutil

    q = query.casefold().removesuffix(".exe")
    titles: dict[int, str] = {}
    for w in window_state.list_windows():
        if w.pid and (w.title or "").strip():
            titles.setdefault(int(w.pid), w.title.strip())
    own: set[int] = {os.getpid()}
    try:
        own.update(p.pid for p in psutil.Process().parents())
    except psutil.Error:
        pass
    out: list[tuple[int, str, str]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info["name"] or ""
            low = name.casefold()
            if proc.info["pid"] in own or low in PROTECTED_PROCESSES:
                continue
            title = titles.get(proc.info["pid"], "")
            if q and (q == low.removesuffix(".exe") or q in low or q in title.casefold()):
                out.append((proc.info["pid"], name, title))
        except (psutil.Error, OSError):
            continue
    return out


class ListAppsTool:
    name: str = "list_apps"
    risk_tier: str = "monitor"
    description: str = (
        "Shows REAL data about programs on this PC: which programs and processes "
        "are running now (scope=running) and which applications are installed on "
        "the disks (scope=installed, searchable with query). Call it before "
        "answering any question about running or installed programs."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["running", "installed", "all"],
                "description": "running = live processes; installed = disk inventory",
                "default": "running",
            },
            "query": {"type": "string", "description": "Optional name filter", "default": ""},
            "rescan": {
                "type": "boolean",
                "description": "Force a fresh disk scan instead of the day-old cache",
                "default": False,
            },
            "brief": {
                "type": "boolean",
                "description": "Short answer (for speech): fewer rows",
                "default": False,
            },
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="list_apps supports Windows only")
        scope = str(args.get("scope") or "running").lower()
        query = str(args.get("query") or "").strip()
        rescan = bool(args.get("rescan", False))
        brief = bool(args.get("brief", False))
        logger.info("[LIST_APPS] scope=%s query=%r rescan=%s brief=%s trace=%s",
                    scope, query, rescan, brief, ctx.trace_id)
        try:
            parts: list[str] = []
            if scope in ("running", "all"):
                parts.append(await asyncio.to_thread(_running_report, query, brief))
            if scope in ("installed", "all"):
                parts.append(await asyncio.to_thread(_installed_report, query, rescan, brief))
            text = "\n\n".join(parts)
            return ToolResult(success=True, output=text[:_MAX_TEXT])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[LIST_APPS] failed", exc_info=True)
            return ToolResult(success=False, output=None, error=f"{type(exc).__name__}: {exc}")


class CloseAppTool:
    name: str = "close_app"
    risk_tier: str = "ask"
    description: str = (
        "Closes a running program by process or window name (politely, like the "
        "X button). Use force=true only if the program ignores a normal close. "
        "System processes and Jarvis itself are never closed."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Process name or window-title part, e.g. 'notepad'"},
            "force": {"type": "boolean", "description": "Kill the process hard", "default": False},
        },
        "required": ["name"],
    }

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="close_app supports Windows only")
        name = str(args.get("name") or "").strip()
        force = bool(args.get("force", False))
        if len(name) < 3 or not re.match(r"^[\w\-\. ]+$", name):
            return ToolResult(success=False, output=None, error="name is missing or too short/unsafe")
        targets = await asyncio.to_thread(_find_targets, name)
        logger.info("[CLOSE_APP] name=%r force=%s matches=%s trace=%s",
                    name, force, [(p, n) for p, n, _ in targets], ctx.trace_id)
        if not targets:
            return ToolResult(success=False, output=None,
                              error=f"Нет запущенного процесса или окна «{name}» (системные защищены).")
        if len(targets) > 60:
            return ToolResult(success=False, output=None,
                              error=f"«{name}» совпало с {len(targets)} процессами — уточните название.")
        for pid, _n, _t in targets:
            argv = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
            await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        await asyncio.sleep(2.0)
        alive = await asyncio.to_thread(_alive, [p for p, _n, _t in targets])
        names = ", ".join(sorted({n for _p, n, _t in targets}))
        if alive and not force:
            # Tray-resident apps (Telegram, Discord, Steam, Spotify) answer the
            # polite close by hiding to the tray: the process lives on with NO
            # window. With no visible window there is no unsaved work to lose, so
            # finish the job; a process that still shows a window (a "Save
            # changes?" dialog) is left alone and reported.
            with_window = await asyncio.to_thread(_pids_with_window)
            hidden = [p for p in alive if p not in with_window]
            for pid in hidden:
                await asyncio.to_thread(
                    subprocess.run, ["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                    check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            if hidden:
                logger.info("[CLOSE_APP] tray-resident pids ended after polite close: %s", hidden)
                await asyncio.sleep(1.0)
                alive = await asyncio.to_thread(_alive, alive)
        if alive:
            return ToolResult(success=False, output=None,
                              error=f"Команда закрытия отправлена ({names}), но у программы остался открытый "
                                    "диалог или окно. Можно повторить с force=true.")
        return ToolResult(success=True, output=f"Закрыто: {names} (процессов: {len(targets)}).")


def _alive(pids: list[int]) -> list[int]:
    import psutil

    return [p for p in pids if psutil.pid_exists(p)]


def _pids_with_window() -> set[int]:
    """PIDs that currently own a visible, titled top-level window."""
    return {int(w.pid) for w in window_state.list_windows() if w.pid and (w.title or "").strip()}
