"""The continuous study runner: no questions in between, "стоп" halts and reports (fork addition)."""
from __future__ import annotations

import asyncio

import pytest

from jarvis.plugins.tool import study_runner as rn


@pytest.fixture(autouse=True)
def _fresh() -> None:
    rn.reset_for_tests()


async def _wait_idle() -> None:
    while rn.is_running():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runs_every_step_until_finished() -> None:
    steps = iter(["a", "b", "c"])
    left = [3]

    async def step():
        left[0] -= 1
        return True, next(steps), left[0] == 0

    msg = await rn.start("тест", step, lambda reason: f"[{reason}]")
    assert "без остановок" in msg
    await _wait_idle()

    assert rn._STATE.lines == ["a", "b", "c"]
    assert "изучение завершено" in rn._STATE.final and "[finished]" in rn._STATE.final


@pytest.mark.asyncio
async def test_stop_halts_after_the_current_step_and_returns_the_report() -> None:
    async def step():
        await asyncio.sleep(0.05)
        return True, "шаг", False

    await rn.start("бесконечно", step, lambda reason: "Продолжить, Сэр?" if reason == "stopped" else "")
    await asyncio.sleep(0.2)
    assert rn.is_running() and "Скажите «стоп»" in rn.status_text()

    report = await rn.stop()

    assert not rn.is_running()
    assert "остановился" in report and "Продолжить, Сэр?" in report
    assert await rn.stop() == report  # a second stop repeats the report


@pytest.mark.asyncio
async def test_three_failures_in_a_row_stop_the_run_and_a_second_start_is_refused() -> None:
    async def failing():
        return False, "нет сети", False

    await rn.start("сбои", failing, lambda reason: reason)
    await _wait_idle()

    assert rn._STATE.reason == "errors" and "нет сети" in rn._STATE.final

    async def slow():
        await asyncio.sleep(10)
        return True, "x", True

    await rn.start("первое", slow, lambda reason: "")
    assert "Уже занят" in await rn.start("второе", slow, lambda reason: "")
    rn._STATE.task.cancel()


def test_stop_word_only_means_stop_while_a_study_runs(tmp_path, monkeypatch) -> None:
    from jarvis.brain.local_action_gate import match_local_action

    monkeypatch.chdir(tmp_path)
    assert match_local_action("стоп").tool_calls[0].args == {"action": "pause"}  # Spotify, nothing is studied


@pytest.mark.asyncio
async def test_the_final_announcement_works_inside_the_running_loop(monkeypatch) -> None:
    """Regression: the announcement used asyncio.run() inside the app loop and crashed the runner's last step."""
    from jarvis.plugins.tool import reminders

    sent: list[str] = []

    class Bus:
        async def publish(self, event) -> None:
            sent.append(event.text)

    monkeypatch.setattr(reminders, "_BUS", Bus())

    async def step():
        return True, "готово", True

    await rn.start("тест", step, lambda reason: "")
    await _wait_idle()
    await asyncio.sleep(0.05)

    assert sent and "изучение завершено" in sent[0]
