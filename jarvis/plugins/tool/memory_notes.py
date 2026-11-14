"""memory_notes: «запомни, что…», «что ты помнишь про…», «что я просил вчера».

Fork addition (2026-11-14). Remembered facts are appended to
``<vault>/jarvis-knowledge/Память.md`` (an Obsidian note). Program and tool names
inside a fact become ``[[wikilinks]]`` so the graph connects what the user said
with what Jarvis can do. Recall searches that note and the other vault notes.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.memory import command_history

_VAULT = Path("wiki/obsidian-vault")
_NOTE = "jarvis-knowledge/Память.md"
_HEADER = "---\ntype: jarvis-memory\n---\n\n# Память\n\nТо, что пользователь просил запомнить. Записи добавляются голосом: «запомни, что…».\n\n"


def _link_names(text: str) -> str:
    """Wrap known program names in wikilinks (Obsidian graph edges)."""
    from jarvis.plugins.tool import app_catalog  # noqa: PLC0415

    try:
        entries = [e for e in app_catalog._load() if e["kind"] in {"lnk", "uwp", "sys"}]  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return text
    spoken = {s: c for s, c in app_catalog.RU_ALIASES.items()}
    out = text
    for e in sorted(entries, key=lambda e: -len(e["name"])):
        name = e["name"]
        words = {name.lower()} | {s for s, c in spoken.items() if c.lower() in {name.lower(), e["key"]}}
        for w in sorted(words, key=len, reverse=True):
            if len(w) < 4:
                continue
            pattern = re.compile(rf"(?<![\w\[])({re.escape(w)})(?![\w\]])", re.I)
            if pattern.search(out):
                out = pattern.sub(rf"[[jarvis-knowledge/Программы/{name}|\1]]", out, count=1)
                break
    return out


def _remember(text: str) -> str:
    note = _VAULT / _NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    if not note.exists():
        note.write_text(_HEADER, encoding="utf-8")
    line = f"- {datetime.now():%Y-%m-%d %H:%M} — {_link_names(text.strip())}\n"
    with note.open("a", encoding="utf-8") as f:
        f.write(line)
    return f"Запомнил: {text.strip()}"


def _learn(topic: str, text: str, source: str, related: list[str], meta: dict[str, str] | None = None) -> str:
    """Write what Jarvis learned as ``Знания/<topic>.md`` and link it to what it already knows."""
    safe = re.sub(r'[\\/:*?"<>|#^\[\]]', " ", topic).strip()[:80]
    if not safe:
        return "У темы нет названия."
    base = _VAULT / "jarvis-knowledge"
    folder = base / "Знания"
    folder.mkdir(parents=True, exist_ok=True)
    note = folder / f"{safe}.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    links = []
    for name in related:
        name = str(name).strip()
        if not name:
            continue
        if (folder / f"{name}.md").exists():
            links.append(f"[[jarvis-knowledge/Знания/{name}|{name}]]")
            continue
        for kind in ("Программы", "Умения"):
            if (base / kind / f"{name}.md").exists():
                links.append(f"[[jarvis-knowledge/{kind}/{name}|{name}]]")
                break
        else:
            links.append(name)
    parts = []
    if not note.exists():
        extra = "".join(f"{k}: {v}\n" for k, v in (meta or {}).items() if v)
        parts.append("---\ntype: jarvis-knowledge\ncreated: " + stamp[:10] + "\n" + extra + "---\n\n# " + safe + "\n\n")
    parts.append("## " + stamp + "\n\n" + _link_names(text.strip()) + "\n\n")
    if source:
        parts.append("Источник: " + source + "\n\n")
    if links:
        parts.append("Связано: " + ", ".join(links) + "\n\n")
    parts.append("Все знания: [[jarvis-knowledge/Знания|Знания]], как я учусь: [[jarvis-knowledge/Обучение|Обучение]]\n\n")
    with note.open("a", encoding="utf-8") as f:
        f.write("".join(parts))
    hub = base / "Знания.md"
    entry = f"- [[jarvis-knowledge/Знания/{safe}|{safe}]]"
    hub_text = hub.read_text(encoding="utf-8") if hub.exists() else "# Знания\n\n"
    if entry not in hub_text:
        hub.write_text(hub_text.rstrip() + "\n" + entry + "\n", encoding="utf-8")
    return f"Записал в знания: {safe}."


def _recall(query: str) -> str:
    tokens = [t for t in re.findall(r"\w{3,}", query.lower())]
    if not tokens:
        return "Что именно вспомнить?"
    hits: list[str] = []
    for path in sorted(_VAULT.rglob("*.md")):
        if "99-templates" in path.parts or ".obsidian" in path.parts or "Программы" in path.parts or "Умения" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            low = line.lower()
            if any(t[:max(3, len(t) - 2)] in low for t in tokens) and line.strip() and not line.startswith(("---", "#")):
                clean = re.sub(r"\[\[[^\]|]*\|?([^\]]*)\]\]", r"\1", line).strip("- ").strip()
                hits.append(f"{path.stem}: {clean[:160]}")
    hits = list(dict.fromkeys(hits))[:8]
    return "Вот что помню:\n" + "\n".join(hits) if hits else f"Про «{query}» в памяти ничего не нашёл."


class MemoryNotesTool:
    name: str = "memory_notes"
    risk_tier: str = "monitor"
    description: str = (
        "Long-term memory in the Obsidian vault. action: remember (text: a fact the user wants kept), "
        "learn (topic, text, source=url, related=[names of tools/programs/other topics]: write what you learned as a linked "
        "knowledge note), recall (query: search the vault and remembered facts), history (days_back: 0 today, 1 yesterday; "
        "the voice commands the user gave)."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["remember", "learn", "recall", "history"]},
            "topic": {"type": "string"}, "source": {"type": "string"},
            "related": {"type": "array", "items": {"type": "string"}},
            "text": {"type": "string"}, "query": {"type": "string"},
            "days_back": {"type": "integer", "minimum": 0, "maximum": 60},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:  # noqa: ARG002
        action = str(args.get("action") or "")
        if action == "remember":
            text = str(args.get("text") or "").strip()
            if not text:
                return ToolResult(success=False, output=None, error="text is empty")
            return ToolResult(success=True, output=await asyncio.to_thread(_remember, text))
        if action == "learn":
            topic, text = str(args.get("topic") or "").strip(), str(args.get("text") or "").strip()
            if not topic or not text:
                return ToolResult(success=False, output=None, error="learn needs topic and text")
            related = [str(x) for x in (args.get("related") or [])]
            return ToolResult(success=True, output=await asyncio.to_thread(
                _learn, topic, text, str(args.get("source") or ""), related))
        if action == "recall":
            return ToolResult(success=True, output=await asyncio.to_thread(_recall, str(args.get("query") or "")))
        if action == "history":
            return ToolResult(success=True, output=command_history.summary(int(args.get("days_back") or 0)))
        return ToolResult(success=False, output=None, error=f"unknown action {action!r}")
