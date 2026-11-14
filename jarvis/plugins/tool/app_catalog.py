"""app_catalog: every program on this PC, found by its spoken (Russian) name.

Fork addition (2026-11-14). The owner wants "open X" to mean "start X's
executable", whatever X is. This module remembers where every program lives:

* Start Menu shortcuts (``.lnk``, with their real target ``.exe``)
* Store / UWP apps (``Get-StartApps``)
* App Paths registry entries
* executables found in Program Files, ``%LOCALAPPDATA%\\Programs`` and the usual
  game folders on every fixed drive

The result is stored in ``data/app_catalog.json`` (rebuilt in the background
when it is older than three days or when a lookup misses). Spoken names are
mapped through a built-in Russian alias table (стим -> steam, спотифай ->
spotify ...), the owner's own ``data/app_aliases.json`` and a Cyrillic ->
Latin transliteration, then matched fuzzily against the catalog.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path("data") / "app_catalog.json"
_USER_ALIASES_PATH = Path("data") / "app_aliases.json"
_TTL_S = 3 * 24 * 3600
_MAX_DEPTH = 3
_SKIP_EXE = re.compile(
    r"unins|uninst|setup|install|update|updater|crash|helper|redist|vcredist|dotnet|"
    r"report|service|elevate|notification|webhelper|cef|gpu|renderer|broker|"
    r"launcher_?crash|diagnos|sentry|bootstrap(?!per$)|squirrel|7z|vc_|dxsetup",
    re.I,
)
_LOCK = threading.Lock()
_entries: list[dict[str, Any]] | None = None
_scan_running = False

#: Spoken name (lower case, ё -> е) -> the name used to find it in the catalog.
#: Keys are what speech recognition produces; values are Latin product names or
#: Windows executable stems.
RU_ALIASES: dict[str, str] = {
    "стим": "steam", "стем": "steam", "стим клиент": "steam",
    "спотифай": "spotify", "спотик": "spotify", "споти фай": "spotify", "спотифай музыка": "spotify",
    "дискорд": "discord", "дискорт": "discord",
    "телеграм": "telegram", "телеграмм": "telegram", "телега": "telegram",
    "вотсап": "whatsapp", "ватсап": "whatsapp", "вацап": "whatsapp", "ватсапп": "whatsapp",
    "хром": "chrome", "гугл хром": "chrome", "гугл": "chrome",
    "фаерфокс": "firefox", "файерфокс": "firefox", "фаир фокс": "firefox", "лиса": "firefox",
    "эдж": "msedge", "едж": "msedge", "майкрософт эдж": "msedge",
    "яндекс браузер": "yandex", "яндекс": "yandex", "браузер": "chrome",
    "опера": "opera", "опера гх": "opera gx", "бравый": "brave", "брейв": "brave",
    "блокнот": "notepad", "ноутпад": "notepad",
    "калькулятор": "calc", "калк": "calc",
    "проводник": "explorer", "эксплорер": "explorer",
    "диспетчер задач": "taskmgr", "таск менеджер": "taskmgr",
    "командная строка": "cmd", "консоль": "cmd", "терминал": "wt", "виндовс терминал": "wt",
    "павершелл": "powershell", "пауэршелл": "powershell", "повершелл": "powershell",
    "пейнт": "mspaint", "паинт": "mspaint", "рисование": "mspaint",
    "ворд": "winword", "эксель": "excel", "эксел": "excel", "пауэрпоинт": "powerpnt", "повер поинт": "powerpnt",
    "аутлук": "outlook", "аутлок": "outlook", "ван ноут": "onenote", "ванноут": "onenote", "тимс": "teams",
    "зум": "zoom", "скайп": "skype", "слэк": "slack", "слак": "slack",
    "обс": "obs", "обс студио": "obs", "обс студия": "obs",
    "влс": "vlc", "влц": "vlc", "вл си": "vlc", "плеер": "vlc",
    "ватсап веб": "whatsapp",
    "визуал студио": "visual studio", "вижуал студио": "visual studio", "вс код": "code",
    "виэс код": "code", "вскод": "code", "визуал студио код": "code",
    "пайчарм": "pycharm", "пучарм": "pycharm", "пай чарм": "pycharm", "интеллиджей": "idea",
    "гитхаб": "github desktop", "гит": "git",
    "докер": "docker desktop", "постман": "postman",
    "ноушен": "notion", "обсидиан": "obsidian", "обсидиан": "obsidian",
    "эпик геймс": "epic games launcher", "эпик": "epic games launcher", "епик": "epic games launcher",
    "баттл нет": "battle.net", "батлнет": "battle.net", "близард": "battle.net",
    "ориджин": "origin", "юплей": "ubisoft connect", "юбисофт": "ubisoft connect",
    "гог": "gog galaxy", "гог гэлакси": "gog galaxy", "ксбокс": "xbox", "иксбокс": "xbox",
    "рокстар": "rockstar games launcher", "рокстар лаунчер": "rockstar games launcher",
    "геншин": "genshin impact", "майнкрафт": "minecraft", "лаунчер майнкрафт": "minecraft launcher",
    "фотошоп": "photoshop", "иллюстратор": "illustrator", "премьер": "premiere", "афтер эффектс": "after effects",
    "блендер": "blender", "давинчи": "davinci resolve", "аудасити": "audacity", "фл студио": "fl studio",
    "кэпкат": "capcut", "капкут": "capcut",
    "хапп": "happ", "хэпп": "happ", "хепп": "happ", "хап": "happ",
    "ватсап десктоп": "whatsapp", "тик ток": "tiktok", "ютуб": "youtube", "ютюб": "youtube",
    "инстаграм": "instagram", "вконтакте": "vk", "вк": "vk", "макс": "max",
    "торрент": "qbittorrent", "кьюбитторрент": "qbittorrent", "утторрент": "utorrent",
    "винрар": "winrar", "семь зип": "7-zip", "7 зип": "7-zip", "зип": "7-zip", "архиватор": "7-zip",
    "пароли": "keepass", "кипас": "keepass", "битвардэн": "bitwarden", "бит варден": "bitwarden",
    "антивирус": "windows security", "защитник": "windows security",
    "параметры": "ms-settings:", "настройки": "ms-settings:", "панель управления": "control",
    "редактор реестра": "regedit", "реестр": "regedit", "службы": "services.msc",
    "диспетчер устройств": "devmgmt.msc", "управление дисками": "diskmgmt.msc",
    "ножницы": "snippingtool", "запись звука": "soundrecorder", "диктофон": "soundrecorder",
    "камера": "camera", "почта": "outlook", "календарь": "outlook", "часы": "clock", "будильник": "clock",
    "магазин": "microsoft store", "майкрософт стор": "microsoft store", "стор": "microsoft store",
    "хуавей": "huawei", "логитех": "logitech g hub", "джи хаб": "logitech g hub", "рэйзер": "razer synapse",
    "синапс": "razer synapse", "эн видиа": "nvidia app", "нвидиа": "nvidia app", "джифорс": "geforce experience",
    "афтербернер": "msi afterburner", "эмэсай афтербернер": "msi afterburner", "хвинфо": "hwinfo",
    "сидисуфи": "cpuid", "цпуз": "cpu-z", "гпуз": "gpu-z", "кристалдиск": "crystaldiskinfo",
    "тимвьювер": "teamviewer", "эни деск": "anydesk", "анидеск": "anydesk", "ректус": "rufus", "руфус": "rufus",
    "эмулятор": "bluestacks", "блюстакс": "bluestacks", "вмвар": "vmware", "виртуалбокс": "virtualbox",
}

try:  # the built-in Windows tools answer to their Russian names too
    from jarvis.plugins.tool.windows_system import spoken_aliases as _sys_aliases

    for _spoken, _canon in _sys_aliases().items():
        if re.search("[а-яё]", _spoken):  # English names keep resolving through the executable resolver
            RU_ALIASES.setdefault(_spoken, _canon)
except Exception:  # noqa: BLE001 - the catalog works without the extras
    pass

_FILLER = re.compile(r"\b(?:пожалуйста|программу|программа|приложение|приложухи|игру|игра|клиент|exe|эксешник|файл)\b")


def _norm(text: str) -> str:
    t = (text or "").lower().replace("ё", "е").replace("_", " ")
    t = re.sub(r"\.exe$", "", t.strip())
    t = re.sub(r"[^\w\s.\-+:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _spoken_key(text: str) -> str:
    t = _norm(text)
    stripped = _FILLER.sub(" ", t)
    return re.sub(r"\s+", " ", stripped).strip() or t


def _translit(text: str) -> str:
    from jarvis.speech.wake_constants import _CYR_MAP  # noqa: PLC0415

    return "".join(_CYR_MAP.get(ch, ch) for ch in text)


def _user_aliases() -> dict[str, str]:
    try:
        raw = json.loads(_USER_ALIASES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {_spoken_key(k): str(v) for k, v in raw.items()}


def add_user_alias(spoken: str, target: str) -> None:
    """Remember ``spoken`` -> ``target`` (an app name or a full path) for good."""
    try:
        raw = json.loads(_USER_ALIASES_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    raw[_spoken_key(spoken)] = target
    _USER_ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USER_ALIASES_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")


def spoken_to_canonical(name: str) -> str:
    """Map a spoken name to its Latin product name; unknown names pass through."""
    key = _spoken_key(name)
    found = _user_aliases().get(key) or RU_ALIASES.get(key)
    if found:
        return found
    for cut in (1, 2, 3):  # "блокноте" -> "блокнот", "в стиме" -> "стим", "дискордом" -> "дискорд"
        if len(key) - cut >= 3:
            found = _user_aliases().get(key[:-cut]) or RU_ALIASES.get(key[:-cut])
            if found and cut <= 3:
                return found
    return name


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------
_PS_SCAN = r"""
$out = New-Object System.Collections.ArrayList
$roots = @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs")
$sh = New-Object -ComObject WScript.Shell
foreach ($r in $roots) {
  if (Test-Path $r) {
    Get-ChildItem -Path $r -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
      $t = ''
      try { $t = $sh.CreateShortcut($_.FullName).TargetPath } catch {}
      [void]$out.Add(@{ n = $_.BaseName; k = 'lnk'; p = $_.FullName; e = $t })
    }
  }
}
try {
  Get-StartApps | ForEach-Object {
    if ($_.AppID -like '*!*') { [void]$out.Add(@{ n = $_.Name; k = 'uwp'; p = 'shell:AppsFolder\' + $_.AppID; e = '' }) }
  }
} catch {}
foreach ($hive in @('HKLM:', 'HKCU:')) {
  $key = "$hive\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
  if (Test-Path $key) {
    Get-ChildItem $key -ErrorAction SilentlyContinue | ForEach-Object {
      $v = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).'(default)'
      if ($v) { $v = $v.Trim('"'); [void]$out.Add(@{ n = ($_.PSChildName -replace '\.exe$',''); k = 'exe'; p = $v; e = $v }) }
    }
  }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 4
"""


def _exe_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value) / "Programs" if var in {"LOCALAPPDATA", "APPDATA"} else Path(value))
    for letter in "CDEFGH":
        drive = Path(f"{letter}:/")
        if not drive.exists():
            continue
        for sub in ("Games", "Program Files", "Program Files (x86)", "Steam/steamapps/common",
                    "SteamLibrary/steamapps/common", "Epic Games", "XboxGames", "Apps", "Programs", "Soft"):
            path = drive / sub
            if path.is_dir():
                roots.append(path)
    seen: set[str] = set()
    unique = []
    for root in roots:
        key = str(root).lower()
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def _walk_exes(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
        for fn in filenames:
            if not fn.lower().endswith(".exe") or _SKIP_EXE.search(fn):
                continue
            full = os.path.join(dirpath, fn)
            found.append({"n": fn[:-4], "k": "scan", "p": full, "e": full})
    return found


def _key(name: str) -> str:
    return _norm(name)


def scan() -> list[dict[str, Any]]:
    """Build the catalog from scratch (blocking, a few seconds) and save it."""
    global _entries  # noqa: PLW0603
    from jarvis.plugins.tool import ui_helpers as ui  # noqa: PLC0415

    raw: list[dict[str, Any]] = []
    try:
        data = json.loads(ui.run_ps(_PS_SCAN, timeout=120) or "[]")
        raw.extend(data if isinstance(data, list) else [data])
    except Exception:  # noqa: BLE001 - a partial catalog is still useful
        logger.warning("app_catalog: shortcut scan failed", exc_info=True)
    try:
        from jarvis.plugins.tool.windows_system import catalog_entries  # noqa: PLC0415

        raw.extend(catalog_entries())
    except Exception:  # noqa: BLE001
        logger.warning("app_catalog: system tools not added", exc_info=True)
    for root in _exe_roots():
        try:
            raw.extend(_walk_exes(root))
        except OSError:
            continue
    best: dict[str, dict[str, Any]] = {}
    rank = {"lnk": 0, "uwp": 1, "sys": 1, "exe": 2, "scan": 3}
    for item in raw:
        name = str(item.get("n") or "").strip()
        path = str(item.get("p") or "").strip()
        if not name or not path:
            continue
        entry = {"name": name, "key": _key(name), "kind": item.get("k", "scan"), "target": path,
                 "exe": str(item.get("e") or "")}
        if item.get("d"):
            entry["desc"] = str(item["d"])
            entry["cmd"] = str(item.get("c") or "")
        prev = best.get(entry["key"])
        if prev is None or rank.get(entry["kind"], 9) < rank.get(prev["kind"], 9):
            best[entry["key"]] = entry
    entries = sorted(best.values(), key=lambda e: e["key"])
    try:
        _CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CATALOG_PATH.write_text(
            json.dumps({"built": time.time(), "entries": entries}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("app_catalog: cannot save the catalog", exc_info=True)
    with _LOCK:
        _entries = entries
    logger.info("app_catalog: %d programs catalogued", len(entries))
    return entries


def _load() -> list[dict[str, Any]]:
    global _entries  # noqa: PLW0603
    with _LOCK:
        if _entries is not None:
            return _entries
    try:
        blob = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
        entries = list(blob.get("entries", []))
        built = float(blob.get("built", 0))
    except (OSError, ValueError, TypeError):
        entries, built = [], 0.0
    with _LOCK:
        _entries = entries
    if not entries or time.time() - built > _TTL_S:
        refresh_in_background()
    return entries


def refresh_in_background() -> None:
    """Rescan on a daemon thread (no-op off Windows or when already running)."""
    global _scan_running  # noqa: PLW0603
    if detect_platform() != "win32":
        return
    with _LOCK:
        if _scan_running:
            return
        _scan_running = True

    def _run() -> None:
        global _scan_running  # noqa: PLW0603
        try:
            scan()
        except Exception:  # noqa: BLE001
            logger.warning("app_catalog: background scan failed", exc_info=True)
        finally:
            _scan_running = False

    threading.Thread(target=_run, name="app-catalog-scan", daemon=True).start()


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def _candidates(spoken: str) -> list[str]:
    key = _spoken_key(spoken)
    out: list[str] = []
    for value in (_user_aliases().get(key), RU_ALIASES.get(key), spoken_to_canonical(spoken), key, _translit(key)):
        if value and _norm(value) not in out:
            out.append(_norm(value))
    return out


def lookup(spoken: str, entries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Best catalog entry for a spoken/written program name, or None."""
    catalog = entries if entries is not None else _load()
    if not catalog or not (spoken or "").strip():
        return None
    by_key = {e["key"]: e for e in catalog}
    keys = list(by_key)
    for cand in _candidates(spoken):
        if cand in by_key:
            return by_key[cand]
        if cand.startswith(("c:", "d:", "e:", "f:")) and os.path.isfile(cand):
            return {"name": cand, "key": cand, "kind": "exe", "target": cand, "exe": cand}
    for cand in _candidates(spoken):
        if len(cand) < 3:
            continue
        starts = [k for k in keys if k.startswith(cand)]
        if starts:
            return by_key[min(starts, key=len)]
        words = set(cand.split())
        contained = [k for k in keys if words and words <= set(re.split(r"[\s\-]+", k))]
        if contained:
            return by_key[min(contained, key=len)]
    for cand in _candidates(spoken):
        if len(cand) < 4:
            continue
        close = difflib.get_close_matches(cand, keys, n=1, cutoff=0.8)
        if close:
            return by_key[close[0]]
    return None


def launch_target_for(spoken: str):  # -> LaunchTarget | None
    """Concrete launch target for a spoken name (used by the app resolver)."""
    from jarvis.plugins.tool.app_resolver import LaunchTarget  # noqa: PLC0415

    entry = lookup(spoken)
    if entry is None:
        return None
    exe = entry.get("exe") or ""
    if entry["kind"] == "sys":
        if exe and os.path.isfile(exe) and not exe.lower().endswith((".msc", ".cpl")):
            return LaunchTarget("executable", exe)
        return LaunchTarget("startfile", entry["target"])
    if entry["kind"] in {"exe", "scan"} and exe and os.path.isfile(exe):
        return LaunchTarget("executable", exe)
    return LaunchTarget("startfile", entry["target"])


def describe(entry: dict[str, Any]) -> str:
    where = entry.get("exe") or entry.get("target")
    return f"{entry['name']} -> {where}"


def search(text: str, limit: int = 12) -> list[dict[str, Any]]:
    """Catalog entries whose name contains the (transliterated) text."""
    catalog = _load()
    out: list[dict[str, Any]] = []
    for cand in _candidates(text):
        for e in catalog:
            if cand in e["key"] and e not in out:
                out.append(e)
    return out[:limit]


class AppCatalogTool:
    """Voice/chat access to the catalog: find, rescan, remember a name or a path."""

    name: str = "app_catalog"
    risk_tier: str = "monitor"
    description: str = (
        "The remembered list of every program on this PC and where its executable lives. "
        "action: find (query = program name, spoken Russian is fine; returns the path), "
        "rescan (rebuild the list), remember (name = what the user calls it, path = full path to the .exe or an "
        "existing program name; saved for good), list (installed programs, optionally filtered by query)."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["find", "rescan", "remember", "list"]},
            "query": {"type": "string"},
            "name": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: Any) -> Any:  # noqa: ARG002
        import asyncio  # noqa: PLC0415

        from jarvis.core.protocols import ToolResult  # noqa: PLC0415

        action = str(args.get("action") or "")
        query = str(args.get("query") or "")
        if action == "rescan":
            entries = await asyncio.to_thread(scan)
            return ToolResult(success=True, output=f"Список обновлён: {len(entries)} программ.")
        if action == "find":
            entry = await asyncio.to_thread(lookup, query)
            if entry is None:
                return ToolResult(success=True, output=f"Программу «{query}» в списке не нашёл.")
            return ToolResult(success=True, output=describe(entry))
        if action == "list":
            rows = await asyncio.to_thread(search, query, 40) if query else [
                e for e in _load() if e["kind"] in {"lnk", "uwp"}][:60]
            return ToolResult(success=True, output="; ".join(e["name"] for e in rows) or "Пусто.")
        if action == "remember":
            name, path = str(args.get("name") or "").strip(), str(args.get("path") or "").strip()
            if not name or not path:
                return ToolResult(success=False, output=None, error="remember needs name and path")
            if os.path.isabs(path) and not os.path.isfile(path):
                return ToolResult(success=False, output=None, error=f"file not found: {path}")
            await asyncio.to_thread(add_user_alias, name, path)
            return ToolResult(success=True, output=f"Запомнил: «{name}» -> {path}.")
        return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
