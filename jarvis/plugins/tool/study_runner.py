"""study_runner: studies go on by themselves until the owner says "стоп".

Fork addition (2026-11-14). One background runner (a task on the app's event loop)
repeats a *step* (one course block, or one program) without asking for permission
in between. The owner interrupts with "стоп": the block in progress finishes, the
runner halts, and ``stop`` returns the report that ends with "Продолжить, Сэр?".
"Продолжить" (or "да", "дальше") then restarts the same study from where it stopped.

Progress is never kept in the runner: courses and programs persist their own state,
so a stopped or crashed runner loses nothing. Only one study runs at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: ``step()`` -> (ok, one-line result, finished). ``ok=False`` counts as a failure; three in a row stop the run.
Step = Callable[[], Awaitable[tuple[bool, str, bool]]]
#: ``after(reason)`` -> tail of the final report; reason: "finished" | "stopped" | "errors".
After = Callable[[str], str]

_MAX_FAILS = 3
_STOP_WAIT_S = 480.0
_STATE: "_Run | None" = None


class _Run:
    def __init__(self, label: str, step: Step, after: After) -> None:
        self.label = label
        self.step = step
        self.after = after
        self.stop = asyncio.Event()
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.started = time.time()
        self.reason = ""
        self.final = ""
        self.user_stopped = False
        self.task: asyncio.Task[None] | None = None


def is_running() -> bool:
    return _STATE is not None and _STATE.task is not None and not _STATE.task.done()


def status_text() -> str:
    if not is_running():
        return ""
    run = _STATE
    assert run is not None
    last = f" Последнее: {run.lines[-1]}" if run.lines else ""
    minutes = int((time.time() - run.started) / 60)
    return (f"Сейчас изучаю подряд: {run.label}. За {minutes} мин. выполнено шагов: {len(run.lines)}.{last} "
            "Скажите «стоп», чтобы остановить.")


def _report(run: _Run) -> str:
    recent = "; ".join(run.lines[-4:]) or "ничего нового"
    head = {"finished": "Сэр, изучение завершено", "stopped": "Сэр, остановился",
            "errors": "Сэр, остановился из-за ошибок"}.get(run.reason, "Сэр, закончил")
    errs = f" Ошибки: {'; '.join(run.failures[-2:])}." if run.failures else ""
    return f"{head}: {run.label}. За сессию шагов: {len(run.lines)}. Последнее: {recent}.{errs} {run.after(run.reason)}".strip()


def _announce(text: str) -> None:
    try:
        from jarvis.core.events import AnnouncementRequested  # noqa: PLC0415
        from jarvis.plugins.tool import reminders  # noqa: PLC0415

        bus = reminders._BUS  # noqa: SLF001 - the bus the timers speak through
        if bus is None:
            return
        event = AnnouncementRequested(text=text, priority="interrupt", language="ru", kind="info")
        asyncio.get_running_loop().create_task(bus.publish(event))  # we are inside the app's loop
    except Exception:  # noqa: BLE001 - the report is still stored
        logger.debug("study_runner: announcement failed", exc_info=True)


async def _loop(run: _Run) -> None:
    fails = 0
    try:
        while not run.stop.is_set():
            try:
                ok, line, finished = await run.step()
            except Exception as exc:  # noqa: BLE001 - one broken step must not kill the runner silently
                logger.warning("study step crashed", exc_info=True)
                ok, line, finished = False, f"сбой: {exc}", False
            if ok:
                fails = 0
                if line:
                    run.lines.append(line)
            else:
                fails += 1
                run.failures.append(line)
                if fails >= _MAX_FAILS:
                    run.reason = "errors"
                    break
            if finished:
                run.reason = "finished"
                break
            await asyncio.sleep(0.3)
        else:
            run.reason = "stopped"
    finally:
        run.reason = run.reason or "stopped"
        run.final = _report(run)
        logger.info("study_runner: %s", run.final)
        if not run.user_stopped:
            _announce(run.final)


async def start(label: str, step: Step, after: After) -> str:
    """Begin a continuous study; returns at once. A running study is not replaced."""
    global _STATE  # noqa: PLW0603
    if is_running():
        return "Уже занят изучением. " + status_text()
    run = _Run(label, step, after)
    _STATE = run
    run.task = asyncio.get_running_loop().create_task(_loop(run), name="study-runner")
    return (f"начал изучать подряд, без остановок: {label}. Скажите «стоп», чтобы остановить: я закончу текущий шаг, "
            "отчитаюсь и спрошу, продолжать ли. «Что изучаешь» покажет прогресс.")


async def stop() -> str:
    """Ask the runner to halt after the current step and return the final report."""
    run = _STATE
    if run is None or run.task is None:
        return "Сейчас ничего не изучаю, Сэр."
    if run.task.done():
        return run.final or "Сейчас ничего не изучаю, Сэр."
    run.user_stopped = True
    run.stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(run.task), timeout=_STOP_WAIT_S)
    except asyncio.TimeoutError:
        return "Остановка запрошена: заканчиваю текущий шаг, отчитаюсь, как только он завершится."
    return run.final


def reset_for_tests() -> None:
    global _STATE  # noqa: PLW0603
    _STATE = None


__all__ = ["Step", "After", "is_running", "start", "status_text", "stop"]
