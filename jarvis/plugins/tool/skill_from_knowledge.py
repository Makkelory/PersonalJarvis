"""skill_from_knowledge: Jarvis turns what it has learned into a real skill.

Fork addition (2026-11-14). The owner wants Jarvis to create skills on its own from
the knowledge it gathered (courses from the web or from files, playbooks of
commands). This tool collects the relevant Obsidian notes (the playbook with exact
commands first), phrases a precise ``intent`` and hands it to the existing
``create-skill`` authoring pipeline, so the result is an ordinary SKILL.md draft
that the Skills view and the trigger matcher already understand.

Safety: like every AI-authored skill it lands as a DRAFT and does nothing until
the owner switches it on (``activate`` here, the Skills view, or ``skill-enable``).
Each created skill gets an Obsidian note that links back to the knowledge it came
from, so the graph shows knowledge -> skill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_VAULT = Path("wiki/obsidian-vault")
_KNOW = "jarvis-knowledge/Знания"
_MAX_CHARS = 8000
_SKIP_LINES = ("Связано:", "Все знания:", "Источник:")


def _strip(text: str) -> str:
    text = re.sub(r"^---.*?---\s*", "", text, count=1, flags=re.S)
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith(_SKIP_LINES)).strip()


def _collect(topic: str, course_title: str = "", module: str = "") -> list[tuple[Path, str]]:
    """Knowledge notes about ``topic`` (playbook first), as (path, cleaned text)."""
    folder = _VAULT / "jarvis-knowledge" / "Знания"
    if not folder.is_dir():
        return []
    tokens = [t for t in re.findall(r"\w{4,}", topic.lower().replace("ё", "е"))]
    scored: list[tuple[int, int, Path, str]] = []
    for path in sorted(folder.glob("*.md")):
        if path.stem.startswith("Курс "):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        low = (path.stem + "\n" + raw).lower().replace("ё", "е")
        head = raw.split("---")[1] if raw.startswith("---") and raw.count("---") >= 2 else ""
        if module and not path.stem.startswith("Приёмы") and f"module: {module}".lower() not in head.lower():
            continue
        score = 0
        if course_title and (course_title.lower() in path.stem.lower() or f"курс {course_title.lower()}" in low):
            score += 10
        for tok in tokens:
            stem = tok[: max(4, len(tok) - 2)]
            score += (3 if stem in path.stem.lower() else 0) + min(low.count(stem), 3)
        if score >= 3:
            playbook = 0 if path.stem.startswith("Приёмы") else 1
            scored.append((playbook, -score, path, _strip(raw)))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [(p, t) for _pb, _s, p, t in scored[:10]]


def _knowledge_text(notes: list[tuple[Path, str]]) -> str:
    parts: list[str] = []
    total = 0
    for path, text in notes:
        chunk = f"## {path.stem}\n{text}"
        if total + len(chunk) > _MAX_CHARS:
            chunk = chunk[: max(_MAX_CHARS - total, 0)]
        if chunk:
            parts.append(chunk)
            total += len(chunk)
        if total >= _MAX_CHARS:
            break
    return "\n\n".join(parts)


def _intent(topic: str, knowledge: str) -> str:
    return (
        f"Навык «{topic}». Создай его на основе знаний Сэра, которые Jarvis изучил (ниже). Когда Сэр просит что-то "
        f"по теме «{topic}», навык действует по этим проверенным шагам и командам. Правила: используй ТОЧНЫЕ команды "
        "и приёмы из текста ниже и не придумывай других; команды PowerShell или терминала выполняй через run_shell; "
        "опасные операции (удаление, форматирование, смена прав) выполняй только по прямой просьбе Сэра и сначала "
        "называй, что именно будет сделано; в конце коротко сообщи результат по-русски на «вы», обращаясь «Сэр».\n\n"
        "ЗНАНИЯ:\n" + knowledge
    )


def _record(name: str, topic: str, notes: list[tuple[Path, str]], draft_path: str) -> None:
    folder = _VAULT / "jarvis-knowledge" / "Навыки"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", name).strip()[:80] or "навык"
    sources = "\n".join(f"- [[jarvis-knowledge/Знания/{p.stem}|{p.stem}]]" for p, _t in notes)
    (folder / f"{safe}.md").write_text(
        f"---\ntype: jarvis-skill\ncreated: {datetime.now():%Y-%m-%d}\nstate: draft\n---\n\n# {safe}\n\n"
        f"Навык, который Jarvis создал сам из знаний по теме «{topic}». Файл навыка: `{draft_path}`.\n\n"
        f"## Из каких знаний\n\n{sources}\n\n"
        "Все навыки: [[jarvis-knowledge/Навыки|Навыки]]. Как Jarvis учится: [[jarvis-knowledge/Обучение|Обучение]].\n",
        encoding="utf-8",
    )
    hub = _VAULT / "jarvis-knowledge" / "Навыки.md"
    entry = f"- [[jarvis-knowledge/Навыки/{safe}|{safe}]]"
    text = hub.read_text(encoding="utf-8") if hub.exists() else (
        "---\ntype: jarvis-hub\n---\n\n# Навыки\n\nНавыки, созданные Jarvis из изученного.\n\n")
    if entry not in text:
        hub.write_text(text.rstrip() + "\n" + entry + "\n", encoding="utf-8")


async def make_skill(topic: str, ctx: Any = None, course_title: str = "", module: str = "") -> tuple[bool, str, dict[str, Any]]:
    """Create a draft skill from the knowledge about ``topic``; returns (ok, message, info)."""
    from jarvis.plugins.tool.create_skill import CreateSkillTool  # noqa: PLC0415

    notes = await asyncio.to_thread(_collect, topic, course_title, module)
    if not notes:
        return False, f"Знаний по теме «{topic}» в Obsidian не нашёл: сначала изучите материалы.", {}
    knowledge = _knowledge_text(notes)
    result = await CreateSkillTool().execute(
        {"intent": _intent(topic, knowledge), "name": topic[:60], "trigger_phrase": "", "schedule": ""}, ctx)
    if not result.success or not isinstance(result.output, dict):
        return False, f"Не удалось создать навык: {result.error or 'ответ без данных'}", {}
    out = result.output
    await asyncio.to_thread(_record, out.get("skill_name", topic), topic, notes, str(out.get("path", "")))
    voice = ", ".join(str(v) for v in (out.get("voice_triggers") or [])[:2]) or "по описанию"
    steps = "; ".join(str(s) for s in (out.get("steps_preview") or [])[:3])
    message = (f"Сэр, создал черновик навыка «{out.get('skill_name')}» на основе {len(notes)} заметок. "
               f"Описание: {out.get('description', '')[:200]} "
               + (f"Шаги: {steps}. " if steps else "")
               + f"Срабатывает: {voice}. Пока он не активен: скажите «включи навык {out.get('skill_name')}» "
               "или включите его в разделе «Навыки».")
    return True, message, out


def _activate(name: str) -> str:
    from jarvis.skills import prefs  # noqa: PLC0415
    from jarvis.skills.skill_context import try_get_skill_context  # noqa: PLC0415

    ctx = try_get_skill_context()
    if ctx is None:
        return "ERROR: Система навыков сейчас недоступна."
    reg = ctx.registry
    names = [s.name for s in reg.list()]
    target = next((n for n in names if n.lower() == name.lower()), None) or next(
        (n for n in names if name.lower() in n.lower()), None)
    if target is None:
        return f"ERROR: Навык «{name}» не найден. Есть: {', '.join(names[:15])}."
    skill = reg.get(target)
    if getattr(skill, "error", None):
        return f"ERROR: Навык «{target}» нельзя включить: {skill.error}"
    try:
        reg.promote(target)
    except Exception as exc:  # noqa: BLE001 - already active or unsafe: say so
        return f"ERROR: Не удалось включить «{target}»: {exc}"
    prefs.set_state(target, True)
    return f"Навык «{target}» включён, Сэр."


class SkillFromKnowledgeTool:
    name: str = "skill_from_knowledge"
    risk_tier: str = "monitor"
    description: str = (
        "Turn learned knowledge into a skill. action: create (topic: what the skill is about, e.g. 'PowerShell и файлы'; "
        "builds a DRAFT skill from the Obsidian knowledge notes and playbooks), auto (create a skill for every finished "
        "file course that has none yet), activate (name: switch a draft skill ON, only when the owner asked). "
        "Never activate on your own."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "auto", "activate"]},
            "topic": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        action = str(args.get("action") or "")
        if action == "activate":
            text = await asyncio.to_thread(_activate, str(args.get("name") or args.get("topic") or ""))
            return (ToolResult(success=False, output=None, error=text[6:].strip())
                    if text.startswith("ERROR:") else ToolResult(success=True, output=text))
        if action == "create":
            topic = str(args.get("topic") or "").strip()
            if topic:
                ok, message, _info = await make_skill(topic, ctx)
                return ToolResult(success=True, output=message) if ok else ToolResult(success=False, output=None, error=message)
            action = "auto"
        if action == "auto":
            try:
                courses = json.loads((Path("data") / "courses.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                courses = {}
            messages: list[str] = []
            for key, course in courses.items():
                if not course.get("source") or course.get("skill") or not course.get("done"):
                    continue
                ok, message, info = await make_skill(course.get("title", key), ctx, course.get("title", ""))
                messages.append(message)
                if ok:
                    course["skill"] = info.get("skill_name", "")
                    (Path("data") / "courses.json").write_text(json.dumps(courses, ensure_ascii=False, indent=1), encoding="utf-8")
                break  # one skill per call: authoring takes a minute
            return ToolResult(success=True, output="\n".join(messages) or
                              "Курсов без навыка нет. Назовите тему: «создай навык из знаний про <тема>».")
        return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
