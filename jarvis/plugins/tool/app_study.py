"""study_app: Jarvis learns how to use the programs it knows about.

Fork addition (2026-11-14). For each program in the catalog it collects what is
needed to use it: a web search for its shortcuts and tasks, and (for a program the
owner names, or one that is already open) the real controls of its window through
``app_control``. A local model writes a short, source-grounded manual; the manual
becomes an Obsidian note "Как пользоваться <программа>" linked to the program's
note, and the exact hotkeys are appended to the playbook "Приёмы Программы" which
the assistant consults before operating a program.

Batch mode studies the next few catalogued programs WITHOUT launching them (no
windows popping up, no game or installer started) and then asks whether to go on.
Progress lives in ``data/app_knowledge.json`` and survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_STORE = Path("data") / "app_knowledge.json"
_VAULT = Path("wiki/obsidian-vault")
_JUNK = re.compile(
    r"uninstall|удал|readme|справк|руководств|license|лиценз|website|веб-?сайт|документац|documentation|install|setup|"
    r"update|обновл|repair|восстановл|crash|redist|visual c\+\+|\.net|launcher helper|configure|настройка запуска|"
    r"driver|драйвер|help|about|release notes|что нового|сообщить|report",
    re.I,
)
_GAME_PATH = re.compile(r"steamapps|steamlibrary|epic games|xboxgames|\\games\\|riot games|battle\.net\\games", re.I)
_SYSTEM_PATH = re.compile(r"\\windows\\(?:system32|syswow64|winsxs)\\", re.I)
_BATCH = 2


def _load() -> dict[str, Any]:
    try:
        blob = json.loads(_STORE.read_text(encoding="utf-8"))
        return blob if isinstance(blob, dict) else {"apps": {}}
    except (OSError, ValueError):
        return {"apps": {}}


def _save(blob: dict[str, Any]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    _STORE.write_text(json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8")


def awaiting() -> bool:
    stamp = _load().get("awaiting")
    return isinstance(stamp, (int, float)) and time.time() - stamp < 20 * 60


def _candidates() -> list[dict[str, Any]]:
    """Catalog programs worth studying, the ones with a Russian spoken name first."""
    from jarvis.plugins.tool import app_catalog  # noqa: PLC0415

    spoken = {v.lower() for v in app_catalog.RU_ALIASES.values()}
    out = []
    for e in app_catalog._load():  # noqa: SLF001
        if e["kind"] not in {"lnk", "uwp", "sys"}:
            continue
        where = f"{e.get('exe', '')} {e.get('target', '')}"
        if _JUNK.search(e["name"]) or _GAME_PATH.search(where) or _SYSTEM_PATH.search(where):
            continue
        if e["kind"] == "lnk" and not e.get("exe"):
            continue  # shortcuts without a program behind them (urls, steam:// game links)
        out.append(e)
    out.sort(key=lambda e: (0 if e["key"] in spoken or e["name"].lower() in spoken else 1, e["name"].lower()))
    return out


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def _resolve(program: str) -> dict[str, Any] | None:
    from jarvis.plugins.tool import app_catalog  # noqa: PLC0415

    entry = app_catalog.lookup(program)
    if entry is not None:
        return entry
    return {"name": program.strip(), "key": _key(program), "kind": "scan", "target": "", "exe": ""} if program.strip() else None


def _summarize(name: str, materials: str, ui_text: str) -> str:
    import httpx  # noqa: PLC0415

    from jarvis.plugins.tool.study_course import _ollama_model  # noqa: PLC0415

    prompt = (
        f"Программа: «{name}». Составь по-русски памятку, как ею пользоваться, ТОЛЬКО по материалам ниже. "
        "Разделы: «Назначение:» (1-2 предложения); «Основные функции:» (список); «Горячие клавиши:» (по строке "
        "«Ctrl+S — сохранить», только те, что есть в материалах, иначе «нет данных»); «Как управлять:» (3-6 "
        "конкретных шагов типичных задач, с названиями кнопок и меню из материалов); «Ключевые термины:» (4-8 через запятую). "
        "Ничего не выдумывай. Если материалов мало, пиши только подтверждённое.\n\n"
        + (f"ЭЛЕМЕНТЫ ИНТЕРФЕЙСА (реально в окне программы):\n{ui_text[:2500]}\n\n" if ui_text else "")
        + "МАТЕРИАЛЫ:\n" + materials[:8000]
    )
    body = {"model": _ollama_model(), "messages": [{"role": "user", "content": prompt}], "stream": False,
            "think": False, "options": {"num_ctx": 12288, "temperature": 0.2}, "keep_alive": "30m"}
    r = httpx.post("http://localhost:11434/api/chat", json=body, timeout=300)
    r.raise_for_status()
    return str(r.json().get("message", {}).get("content", "")).strip()


async def _inspect_ui(name: str, launch: bool) -> str:
    """Real controls of the program's window; launches it first when asked and not running."""
    from jarvis.plugins.tool import app_control  # noqa: PLC0415

    if not app_control._window(name):  # noqa: SLF001
        if not launch:
            return ""
        from jarvis.plugins.tool.open_app import OpenAppTool  # noqa: PLC0415

        res = await OpenAppTool().execute({"app_name": name}, None)
        if not getattr(res, "success", False):
            return ""
        for _ in range(12):
            await asyncio.sleep(1.0)
            if app_control._window(name):  # noqa: SLF001
                break
        else:
            return ""
        await asyncio.sleep(2.0)
    text = await asyncio.to_thread(app_control._inspect, name)  # noqa: SLF001
    return text if "элемент" in text or "OCR" in text else ""


async def study_one(entry: dict[str, Any], *, launch: bool) -> tuple[str, str]:
    """Study one program; returns (status, report line)."""
    from jarvis.plugins.tool import study_course as sc  # noqa: PLC0415
    from jarvis.plugins.tool.memory_notes import _learn  # noqa: PLC0415

    name = entry["name"]
    ui_text = await _inspect_ui(name, launch)
    queries = [f"{name} как пользоваться", f"{name} горячие клавиши", f"{name} keyboard shortcuts guide"]
    materials, sources = await sc._gather({"title": name, "module": "программа"}, queries)  # noqa: SLF001
    if len(materials) < 300 and not ui_text:
        return "no_data", f"{name}: материалов не нашёл"
    summary = await asyncio.to_thread(_summarize, name, materials, ui_text)
    if len(summary) < 150:
        return "failed", f"{name}: модель не составила памятку"
    terms: list[str] = []
    m = re.search(r"Ключевые термины:\s*(.+)", summary)
    if m:
        terms = [t.strip(" .") for t in m.group(1).split(",") if t.strip(" .")]
        summary = summary[: m.start()].strip()
    hotkeys = ""
    hm = re.search(r"Горячие клавиши:\s*(.*?)(?=\n\s*Как управлять:|\Z)", summary, re.S)
    if hm and "нет данных" not in hm.group(1).lower():
        hotkeys = hm.group(1).strip()
    body = summary + (("\n\nКлючевые термины: " + ", ".join(terms)) if terms else "")
    if ui_text:
        body += "\n\nЭлементы интерфейса, найденные в окне:\n" + ui_text[:1800]
    safe = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", name).strip()
    _learn(f"Как пользоваться {safe}", body, ", ".join(sources[:2]), [safe, "app_control"],
           {"course": "Программы", "module": "Как пользоваться", "program": safe})
    if hotkeys:
        folder = _VAULT / "jarvis-knowledge" / "Знания"
        playbook = folder / "Приёмы Программы.md"
        if not playbook.exists():
            playbook.write_text("---\ntype: jarvis-playbook\ncourse: Программы\n---\n\n# Приёмы Программы\n\n"
                                "Горячие клавиши и приёмы работы с программами. Jarvis сверяется с ними перед работой.\n\n",
                                encoding="utf-8")
        with playbook.open("a", encoding="utf-8") as f:
            f.write(f"## {safe}\n\n{hotkeys}\n\nПодробнее: [[jarvis-knowledge/Знания/Как пользоваться {safe}|Как пользоваться {safe}]]\n\n")
    blob = _load()
    blob.setdefault("apps", {})[_key(name)] = {"name": name, "status": "done", "at": time.time(),
                                               "ui": bool(ui_text), "hotkeys": bool(hotkeys)}
    _save(blob)
    return "done", f"{name}: памятка готова" + (" (с горячими клавишами)" if hotkeys else "") + (", окно изучено" if ui_text else "")


class StudyAppTool:
    name: str = "study_app"
    risk_tier: str = "monitor"
    description: str = (
        "Learn how to use programs. action: study (program: one program; opens it if closed and reads its real "
        "controls, searches the web for shortcuts, writes a manual into Obsidian), study_next (the next few catalogued "
        "programs one after another IN THE BACKGROUND without launching them and without asking in between; "
        "'стоп' halts it), status."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["study", "study_next", "status"]},
            "program": {"type": "string"},
            "count": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:  # noqa: ARG002
        action = str(args.get("action") or "")
        try:
            if action == "status":
                done = [a for a in _load().get("apps", {}).values() if a.get("status") == "done"]
                total = len(_candidates())
                return ToolResult(success=True, output=f"Изучено программ: {len(done)} из {total} подходящих в каталоге.")
            if action == "study":
                program = str(args.get("program") or "").strip()
                entry = _resolve(program)
                if entry is None:
                    return ToolResult(success=False, output=None, error="Назовите программу.")
                status, line = await study_one(entry, launch=True)
                ok = status == "done"
                text = f"Сэр, {line}. Заметка «Как пользоваться {entry['name']}» в Obsidian." if ok else f"Не получилось: {line}."
                return ToolResult(success=ok, output=text if ok else None, error=None if ok else text)
            if action == "study_next":
                from jarvis.plugins.tool import study_runner  # noqa: PLC0415

                def _todo() -> list[dict[str, Any]]:
                    known = _load().get("apps", {})
                    return [e for e in _candidates() if _key(e["name"]) not in known]

                if not _todo():
                    return ToolResult(success=True, output="Все подходящие программы из каталога уже изучены, Сэр.")

                async def step() -> tuple[bool, str, bool]:
                    todo = _todo()
                    if not todo:
                        return True, "", True
                    e = todo[0]
                    status, line = await study_one(e, launch=False)
                    if status != "done":
                        blob = _load()
                        blob.setdefault("apps", {})[_key(e["name"])] = {"name": e["name"], "status": status, "at": time.time()}
                        _save(blob)
                    return status != "failed", line, len(_todo()) == 0

                def after(reason: str) -> str:
                    left = len(_todo())
                    blob = _load()
                    blob["awaiting"] = time.time() if left and reason != "finished" else None
                    _save(blob)
                    return "Все программы из каталога пройдены." if not left else f"Осталось программ: {left}. Продолжить, Сэр?"

                msg = await study_runner.start("программы из каталога", step, after)
                return ToolResult(success=True, output=f"Сэр, {msg} В очереди программ: {len(_todo())}.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("study_app %s failed", action, exc_info=True)
            return ToolResult(success=False, output=None, error=f"study_app {action}: {exc}")
        return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
