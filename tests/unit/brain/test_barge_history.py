"""A barged-over answer is still remembered (fork addition)."""
from __future__ import annotations

import asyncio

import pytest

from jarvis.brain.manager import BrainManager
from jarvis.core.config import JarvisConfig
from jarvis.core.bus import EventBus


@pytest.mark.asyncio
async def test_the_cancelled_stream_keeps_the_question_and_the_part_that_was_said() -> None:
    manager = BrainManager(config=JarvisConfig(), bus=EventBus(), tools={})

    async def _slow_generate(user_text, *, text_consumer=None, **kwargs):
        text_consumer("Сначала про диски. ")
        text_consumer("Потом про службы. ")
        await asyncio.sleep(30)  # the owner interrupts long before this ends
        return "never"

    manager.generate = _slow_generate  # type: ignore[method-assign]
    stream = manager.generate_stream("расскажи про Windows")

    got = await stream.__anext__()
    assert got.startswith("Сначала")
    await stream.aclose()  # what the speech pipeline does when the owner speaks over the answer

    assert [m.role for m in manager._history[-2:]] == ["user", "assistant"]
    assert manager._history[-2].content == "расскажи про Windows"
    assert "Сначала про диски." in manager._history[-1].content
    assert "Сэр перебил этот ответ" in manager._history[-1].content
