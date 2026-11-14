"""scenes: one phrase runs a saved list of actions ("игровой режим", "тишина").

Fork addition (2026-11-14). A scene is a named list of ``{"tool", "args"}``
steps stored in ``data/scenes.json`` (a few defaults ship in code; the file
overrides them). The model can create new ones from a chat request through the
``save`` action. Steps run through the same local tools, restricted to a safe
set: nothing that powers off the PC, deletes files or runs shell commands.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jarvis.core.protocols import ExecutionContext, ToolResult

logger = logging.getLogger(__name__)

_STORE = Path("data") / "scenes.json"
_ALLOWED = {"open_app", "close_app", "set_volume", "spotify_control", "steam_control", "happ_vpn",
            "window_control", "browser_control", "app_catalog", "set_timer"}
_DEFAULTS: dict[str, list[dict[str, Any]]] = {
    "игровой": [
        {"tool": "open_app", "args": {"app_name": "steam"}},
        {"tool": "spotify_control", "args": {"action": "play"}},
        {"tool": "set_volume", "args": {"action": "set", "level": 40}},
    ],
    "тишина": [
        {"tool": "spotify_control", "args": {"action": "pause"}},
        {"tool": "set_volume", "args": {"action": "mute"}},
    ],
}
_tools: dict[str, Any] = {}


def bind(tools: dict[str, Any]) -> None:
    """Give the scene runner the registry of local tools (called by the factory)."""
    _tools.clear()
    _tools.update(tools)


def _load() -> dict[str, list[dict[str, Any]]]:
    scenes = {k: list(v) for k, v in _DEFAULTS.items()}
    try:
        blob = json.loads(_STORE.read_text(encoding="utf-8"))
        if isinstance(blob, dict):
            scenes.update({str(k).lower(): v for k, v in blob.items() if isinstance(v, list)})
    except (OSError, ValueError):
        pass
    return scenes


def names() -> list[str]:
    return sorted(_load())


def resolve(spoken: str) -> str | None:
    """Scene name for a spoken phrase ("игровой режим", "режим тишина"), or None."""
    text = " ".join((spoken or "").lower().replace("ё", "е").split())
    for noise in ("режим", "сценарий"):
        text = text.replace(noise, " ")
    text = " ".join(text.split())
    known = _load()
    if text in known:
        return text
    starts = [n for n in known if n.startswith(text) or text.startswith(n)] if len(text) >= 3 else []
    return min(starts, key=len) if starts else None


class ScenesTool:
    name: str = "scenes"
    risk_tier: str = "monitor"
    description: str = (
        "Named one-phrase scenarios. action: run (name), list, save (name + steps: list of {tool, args}; allowed "
        f"tools: {', '.join(sorted(_ALLOWED))}), delete (name). Defaults: игровой, тишина."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["run", "list", "save", "delete"]},
            "name": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "tool": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool"]}},
        },
        "required": ["action"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str:  # noqa: ARG002
        return "monitor"

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        action = str(args.get("action") or "")
        if action == "list":
            return ToolResult(success=True, output="Сценарии: " + ", ".join(names()))
        raw_name = str(args.get("name") or "").strip()
        if action == "run":
            name = resolve(raw_name)
            if name is None:
                return ToolResult(success=False, output=None,
                                  error=f"Нет сценария «{raw_name}». Есть: {', '.join(names())}.")
            return ToolResult(success=True, output=await self._run(name, ctx))
        if action == "save":
            steps = args.get("steps")
            name = raw_name.lower().replace("режим", "").strip()
            if not name or not isinstance(steps, list) or not steps:
                return ToolResult(success=False, output=None, error="save needs a name and a non-empty steps list")
            bad = [s.get("tool") for s in steps if not isinstance(s, dict) or s.get("tool") not in _ALLOWED]
            if bad:
                return ToolResult(success=False, output=None, error=f"tools not allowed in scenes: {bad}")
            try:
                blob = json.loads(_STORE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                blob = {}
            blob[name] = [{"tool": s["tool"], "args": s.get("args") or {}} for s in steps]
            _STORE.parent.mkdir(parents=True, exist_ok=True)
            _STORE.write_text(json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8")
            return ToolResult(success=True, output=f"Сохранил сценарий «{name}» ({len(steps)} шагов).")
        if action == "delete":
            name = raw_name.lower()
            try:
                blob = json.loads(_STORE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                blob = {}
            if name not in blob:
                return ToolResult(success=False, output=None, error=f"«{name}» не мой собственный сценарий или не найден")
            del blob[name]
            _STORE.write_text(json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8")
            return ToolResult(success=True, output=f"Удалил сценарий «{name}».")
        return ToolResult(success=False, output=None, error=f"unknown action {action!r}")

    async def _run(self, name: str, ctx: ExecutionContext) -> str:
        results: list[str] = []
        for step in _load()[name]:
            tool = _tools.get(step.get("tool", ""))
            if tool is None or step.get("tool") not in _ALLOWED:
                results.append(f"✗ {step.get('tool')}: недоступен")
                continue
            try:
                res = await tool.execute(dict(step.get("args") or {}), ctx)
            except Exception as exc:  # noqa: BLE001
                results.append(f"✗ {step['tool']}: {exc}")
                continue
            ok = bool(getattr(res, "success", False))
            body = getattr(res, "output", None) if ok else getattr(res, "error", "")
            results.append(f"{'✓' if ok else '✗'} {step['tool']}: {str(body)[:120]}")
        return f"Сценарий «{name}»:\n" + "\n".join(results)
