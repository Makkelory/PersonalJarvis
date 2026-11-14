"""file_ops: what a classic user does in Explorer — list, find, open, read, write,
create folders, copy, move, rename and delete (to the Recycle Bin).

Fork addition (2026-11-14). Safety is built in, independent of the approval
setting: deletes always go to the Recycle Bin (reversible), and system
locations (Windows, Program Files, ProgramData, drive roots, the profile root,
the Jarvis folder itself) can never be written, moved or deleted.

Paths may be written with Russian folder words ("рабочий стол", "загрузки",
"документы", "картинки", "музыка", "видео") or ``~``/``%VAR%``.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import shutil
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.platform import detect_platform

logger = logging.getLogger(__name__)

_MAX_TEXT = 3000
_FIND_BUDGET_S = 25.0
_TEXT_SUFFIXES = {".txt", ".md", ".log", ".json", ".csv", ".ini", ".cfg", ".toml",
                  ".yaml", ".yml", ".py", ".js", ".html", ".xml", ".bat", ".ps1", ".sh"}
_SKIP_DIRS = {"$recycle.bin", "system volume information", "windows", "node_modules",
              ".git", "__pycache__", "winsxs", "appdata"}

#: Spoken folder word -> Windows "Shell Folders" registry value.
_KNOWN = {
    "рабочий стол": "Desktop", "рабочего стола": "Desktop", "desktop": "Desktop",
    "документы": "Personal", "документы.": "Personal", "документах": "Personal", "documents": "Personal",
    "загрузки": "{374DE290-123F-4565-9164-39C4925E467B}", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "картинки": "My Pictures", "изображения": "My Pictures", "фото": "My Pictures", "pictures": "My Pictures",
    "музыка": "My Music", "музыку": "My Music", "music": "My Music",
    "видео": "My Video", "videos": "My Video",
}


def known_folder(word: str) -> Path | None:
    """Resolve a (Russian or English) folder word to its real path, or None."""
    key = _KNOWN.get(re.sub(r"\s+", " ", word.strip().lower()))
    if key is None:
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as k:
            return Path(os.path.expandvars(str(winreg.QueryValueEx(k, key)[0])))
    except OSError:
        return None


def resolve_path(raw: str) -> Path:
    """Spoken/relative path -> absolute Path (not required to exist)."""
    text = os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"')))
    head, sep, tail = text.replace("/", "\\").partition("\\")
    base = known_folder(head)
    if base is not None:
        return base / tail if sep else base
    folder = known_folder(text)
    if folder is not None:
        return folder
    p = Path(text)
    if not p.is_absolute():
        p = (known_folder("рабочий стол") or Path.home()) / p
    return p


def _protected_reason(path: Path) -> str | None:
    """Why ``path`` must never be changed, or None when it is fair game."""
    try:
        p = path.resolve()
    except OSError:
        p = path
    low = str(p).rstrip("\\").lower()
    if len(low) <= 3:
        return "корень диска"
    roots = [os.environ.get(v, "") for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
    for root in roots:
        r = root.rstrip("\\").lower()
        if r and (low == r or low.startswith(r + "\\")):
            return "системная папка"
    profile = str(Path.home()).lower()
    if low in (profile, str(Path(profile).parent)):
        return "корневая папка пользователя"
    jarvis_root = str(Path(__file__).resolve().parents[3]).lower()
    if low == jarvis_root or low.startswith(jarvis_root + "\\"):
        return "папка самого Jarvis"
    if "$recycle.bin" in low or "system volume information" in low:
        return "служебная папка"
    return None


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _recycle(path: Path) -> None:
    """Move a file or folder to the Recycle Bin (silent, undoable)."""
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = str(path) + "\0"  # double-null terminated (ctypes adds the second)
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"не удалось переместить в корзину (код {rc})")


def _fmt_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} Б"


def _describe(p: Path) -> str:
    try:
        st = p.stat()
        when = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")
        if p.is_dir():
            return f"[папка] {p.name}  ({when})"
        return f"{p.name}  {_fmt_size(st.st_size)}  ({when})"
    except OSError:
        return p.name


def _fixed_roots() -> list[Path]:
    from jarvis.plugins.tool.system_apps import _fixed_drives

    return [Path(d) for d in _fixed_drives()]


def _find(query: str, root: Path | None, limit: int) -> list[Path]:
    q = query.casefold()
    if root is not None:
        roots = [root]
    else:
        user = [p for p in (known_folder(w) for w in ("рабочий стол", "документы", "загрузки",
                                                          "картинки", "музыка", "видео")) if p]
        roots = user + _fixed_roots()
    deadline = time.monotonic() + _FIND_BUDGET_S
    found: list[Path] = []
    seen: set[str] = set()
    for base in roots:
        stack = [base]
        while stack and len(found) < limit:
            if time.monotonic() > deadline:
                return found
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        name = entry.name
                        if q in name.casefold():
                            full = str(Path(entry.path))
                            if full.lower() not in seen:
                                seen.add(full.lower())
                                found.append(Path(entry.path))
                                if len(found) >= limit:
                                    break
                        if entry.is_dir(follow_symlinks=False) and name.lower() not in _SKIP_DIRS:
                            stack.append(Path(entry.path))
            except (PermissionError, OSError):
                continue
    return found


class FileOpsTool:
    name: str = "file_ops"
    risk_tier: str = "monitor"
    description: str = (
        "Works with files and folders like Explorer. action: list (path), find "
        "(query, optional path), open (path), read (path), write (path, text, "
        "optional append), mkdir (path), copy (path, dest), move (path, dest), "
        "rename (path, dest = new name), delete (path, goes to the Recycle Bin). "
        "Paths may use words: 'рабочий стол', 'загрузки', 'документы', 'картинки', "
        "'музыка', 'видео'. System folders are protected."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["list", "find", "open", "read", "write", "mkdir", "copy", "move", "rename", "delete"]},
            "path": {"type": "string", "description": "File or folder; folder words allowed"},
            "dest": {"type": "string", "description": "Destination (copy/move) or new name (rename)"},
            "query": {"type": "string", "description": "Name fragment for find"},
            "text": {"type": "string", "description": "Content for write"},
            "append": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 30},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:
        """Reads and opens are benign; anything that changes or removes data is ``ask``."""
        action = str(args.get("action") or "list").lower()
        return "ask" if action in {"write", "move", "rename", "delete", "copy"} else "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if detect_platform() != "win32":
            return ToolResult(success=False, output=None, error="file_ops supports Windows only")
        action = str(args.get("action") or "").lower()
        logger.info("[FILE_OPS] action=%s path=%r dest=%r query=%r trace=%s", action,
                    args.get("path"), args.get("dest"), args.get("query"), ctx.trace_id)
        try:
            text = await asyncio.to_thread(self._run, action, args)
            return ToolResult(success=True, output=text[:_MAX_TEXT])
        except (OSError, ValueError, PermissionError) as exc:
            return ToolResult(success=False, output=None, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    def _run(self, action: str, args: dict[str, Any]) -> str:  # noqa: C901 — flat dispatch
        limit = max(1, min(100, int(args.get("limit") or 30)))
        raw = str(args.get("path") or "").strip()
        if action == "find":
            query = str(args.get("query") or raw).strip()
            if len(query) < 2:
                raise ValueError("query is too short")
            root = resolve_path(raw) if raw and args.get("query") else None
            hits = _find(query, root, min(limit, 15))
            if not hits:
                return f"Файлов и папок со словом «{query}» не найдено (поиск до {_FIND_BUDGET_S:.0f} с)."
            return f"Найдено {len(hits)}:\n" + "\n".join(f"- {h}  — {_describe(h)}" for h in hits)
        if not raw:
            raise ValueError("path is required")
        path = resolve_path(raw)
        if action == "list":
            if not path.is_dir():
                raise ValueError(f"{path} — не папка или не существует")
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
            lines = [f"{path} — элементов: {len(entries)}"]
            lines += [f"- {_describe(e)}" for e in entries[:limit]]
            if len(entries) > limit:
                lines.append(f"…и ещё {len(entries) - limit}")
            return "\n".join(lines)
        if action == "open":
            if not path.exists():
                raise ValueError(f"{path} не существует")
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606 — user-requested open
            return f"Открыл: {path}"
        if action == "read":
            if not path.is_file():
                raise ValueError(f"{path} — не файл или не существует")
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                raise ValueError(f"читаю только текстовые файлы ({', '.join(sorted(_TEXT_SUFFIXES))})")
            data = path.read_bytes()[:1_000_000]
            for enc in ("utf-8-sig", "cp1251"):
                try:
                    return data.decode(enc)[:_MAX_TEXT]
                except UnicodeDecodeError:
                    continue
            raise ValueError("не удалось определить кодировку")
        # ---- everything below changes the disk: protected locations are off limits
        if action in {"write", "mkdir", "move", "rename", "delete"}:
            reason = _protected_reason(path)
            if reason:
                raise PermissionError(f"{path}: {reason}, менять нельзя")
        if action == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
            return f"Папка готова: {path}"
        if action == "write":
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                raise ValueError("пишу только текстовые файлы")
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if args.get("append") else "w"
            with open(path, mode, encoding="utf-8", newline="") as fh:
                fh.write(str(args.get("text") or ""))
            return f"Записал в {path} ({_fmt_size(path.stat().st_size)})"
        if action == "delete":
            if not path.exists():
                raise ValueError(f"{path} не существует")
            _recycle(path)
            return f"Перемещено в корзину: {path} (можно восстановить из корзины)"
        dest_raw = str(args.get("dest") or "").strip()
        if not dest_raw:
            raise ValueError("dest is required")
        if action == "rename":
            if re.search(r'[\\/:*?"<>|]', dest_raw):
                raise ValueError("новое имя не должно содержать \\ / : * ? \" < > |")
            target = path.with_name(dest_raw)
            if target.exists():
                raise ValueError(f"{target} уже существует")
            path.rename(target)
            return f"Переименовано: {path.name} → {target.name}"
        target = resolve_path(dest_raw)
        # A destination that exists as a folder, or a not-yet-existing name
        # without an extension for a file, means "put it INSIDE that folder".
        if target.is_dir() or (path.is_file() and not target.exists() and not target.suffix):
            target = target / path.name
        reason = _protected_reason(target)
        if reason:
            raise PermissionError(f"{target}: {reason}, менять нельзя")
        if target.exists():
            raise ValueError(f"{target} уже существует, ничего не перезаписываю")
        if not path.exists():
            raise ValueError(f"{path} не существует")
        target.parent.mkdir(parents=True, exist_ok=True)
        if action == "copy":
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)
            return f"Скопировано: {path} → {target}"
        if action == "move":
            shutil.move(str(path), str(target))
            return f"Перемещено: {path} → {target}"
        raise ValueError(f"unknown action {action!r}")
