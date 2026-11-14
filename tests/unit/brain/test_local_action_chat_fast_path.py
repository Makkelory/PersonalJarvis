"""The DIRECT local-action fast path also serves the interactive typed chat (fork addition).

Scoped surfaces (society browser, local-models assistant, bare overrides) and plan
mode must keep refusing the global desktop shortcut.
"""
from __future__ import annotations

import pytest

from jarvis.brain.manager import _TURN_OVERRIDE
from jarvis.brain.turn_override import TurnOverride
from tests.unit.brain.test_local_action_ack_language import (
    _direct_open_explorer_plan,
    _make_direct_manager,
)


class _RecordingExecutor:
    """Accepts the optional ``config_snapshot`` the chat path passes."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def execute(self, tool, args, **kwargs):  # noqa: ANN001, ANN003
        from types import SimpleNamespace

        self.kwargs = dict(kwargs)
        return SimpleNamespace(success=True, output=f"Gestartet: {args.get('app_name')}", error=None)


def _chat_override(**context: object) -> TurnOverride:
    base = {"approval_surface": "interactive", "tool_origin": "agent-chat", "delivery": "written"}
    return TurnOverride(provider="ollama", tools_extra={"folder_tool": object()}, tool_context={**base, **context})


async def _run(override: TurnOverride | None, monkeypatch) -> tuple[object, _RecordingExecutor]:
    manager = _make_direct_manager(reply_language="en")
    executor = _RecordingExecutor()
    manager._tool_executor = executor
    monkeypatch.setattr("jarvis.brain.manager.match_local_action", _direct_open_explorer_plan)
    token = _TURN_OVERRIDE.set(override)
    try:
        return await manager._run_local_action_fast_path("open the explorer"), executor
    finally:
        _TURN_OVERRIDE.reset(token)


@pytest.mark.asyncio
async def test_interactive_chat_runs_the_shortcut_with_its_tool_context(monkeypatch) -> None:
    result, executor = await _run(_chat_override(), monkeypatch)

    assert result is not None
    assert executor.kwargs is not None
    assert executor.kwargs["config_snapshot"]["approval_surface"] == "interactive"


@pytest.mark.asyncio
async def test_plan_mode_chat_never_runs_the_shortcut(monkeypatch) -> None:
    result, executor = await _run(_chat_override(chat_read_only=True), monkeypatch)

    assert result is None
    assert executor.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["society", "local-models-assistant"])
async def test_scoped_surfaces_never_run_the_shortcut(origin: str, monkeypatch) -> None:
    result, executor = await _run(_chat_override(tool_origin=origin), monkeypatch)

    assert result is None
    assert executor.kwargs is None


@pytest.mark.asyncio
async def test_voice_path_keeps_its_original_call_shape(monkeypatch) -> None:
    result, executor = await _run(None, monkeypatch)

    assert result is not None
    assert "config_snapshot" not in (executor.kwargs or {})
