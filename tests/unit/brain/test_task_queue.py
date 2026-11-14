"""A stream of tasks in one utterance runs in order, even when a step fails (fork addition)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.brain.local_action_gate import (
    LocalActionMode,
    LocalActionPlan,
    LocalToolCall,
    match_local_action,
    split_into_commands,
)
from tests.unit.brain.test_local_action_ack_language import _make_direct_manager

QUEUE = "открой стим, открой проводник, включи музыку, включи радио по этому треку, поставь громкость 60%"


def test_the_example_queue_becomes_five_ordered_steps() -> None:
    plan = match_local_action(QUEUE)

    assert plan is not None and plan.compound and plan.mode == LocalActionMode.DIRECT
    assert [c.name for c in plan.tool_calls] == ["open_app", "open_app", "spotify_control", "spotify_control", "set_volume"]
    assert plan.tool_calls[1].args == {"app_name": "проводник"}
    assert plan.tool_calls[3].args == {"action": "radio"}
    assert plan.tool_calls[4].args == {"action": "set", "level": 60}


def test_recogniser_text_without_punctuation_is_split_at_the_command_verbs() -> None:
    text = "открой стим открой проводник включи музыку поставь громкость шестьдесят процентов"

    assert split_into_commands(text) == ["открой стим", "открой проводник", "включи музыку", "поставь громкость шестьдесят процентов"]
    plan = match_local_action(text)
    assert plan is not None and [c.name for c in plan.tool_calls] == ["open_app", "open_app", "spotify_control", "set_volume"]


@pytest.mark.parametrize("text,count", [
    ("открой стим, проводник, блокнот", 3),
    ("джарвис открой стим и проводник", 2),
    ("открой стим потом включи музыку затем громкость пятьдесят", 3),
    ("перенеси браузер на второй монитор и сверни телеграм", 2),
])
def test_other_ways_to_chain_tasks(text: str, count: int) -> None:
    plan = match_local_action(text)

    assert plan is not None and plan.compound and len(plan.tool_calls) == count


@pytest.mark.parametrize("text", ["найди файл договор и акт", "открой блокнот", "включи музыку", "запомни что нужно открыть стим и включить музыку"])
def test_single_commands_and_unclear_chains_are_left_alone(text: str) -> None:
    plan = match_local_action(text)

    assert plan is None or not plan.compound


def test_a_chain_with_a_step_the_gate_does_not_know_goes_to_the_model_whole() -> None:
    assert match_local_action("открой хром и найди погоду") is None


class _FlakyExecutor:
    """The second step fails, the others succeed; records the order."""

    def __init__(self) -> None:
        self.order: list[str] = []

    async def execute(self, tool, args, **kwargs):  # noqa: ANN001, ANN003
        self.order.append(str(args))
        if args.get("app_name") == "broken":
            return SimpleNamespace(success=False, output=None, error="exit 1")
        return SimpleNamespace(success=True, output=f"ok {args}", error=None)


@pytest.mark.asyncio
async def test_the_fast_path_runs_every_step_and_does_not_stop_at_a_failure(monkeypatch) -> None:
    manager = _make_direct_manager(reply_language="ru")
    manager._local_action_tools = {"open_app": object(), "set_volume": object()}
    executor = _FlakyExecutor()
    manager._tool_executor = executor
    published: list[object] = []

    class _Bus:
        async def publish(self, event) -> None:  # noqa: ANN001
            published.append(event)

    manager._bus = _Bus()
    plan = LocalActionPlan(
        mode=LocalActionMode.DIRECT, compound=True, steps=("a", "b", "c"),
        tool_calls=(LocalToolCall("open_app", {"app_name": "steam"}), LocalToolCall("open_app", {"app_name": "broken"}),
                    LocalToolCall("set_volume", {"action": "set", "level": 60})),
    )
    monkeypatch.setattr("jarvis.brain.manager.match_local_action", lambda *a, **k: plan)

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("jarvis.brain.manager.asyncio.sleep", _no_sleep)

    async def _failure(result, **kwargs):  # noqa: ANN001, ANN003
        return "не вышло"

    manager._honest_failure_readback = _failure  # type: ignore[method-assign]

    text = await manager._run_local_action_fast_path("открой стим, открой broken, громкость 60")

    assert len(executor.order) == 3  # all three steps ran
    assert "Шаг 2: не вышло" in text and "ok" in text
    assert [e.text for e in published if hasattr(e, "text")][:1] == ["Принял, Сэр. Шагов: 3. Выполняю по порядку."]
