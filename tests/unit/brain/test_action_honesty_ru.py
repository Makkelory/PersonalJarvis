"""Russian coverage for the execution-state guards (fork addition).

The guard only ever knew German, English and Spanish, so a Russian turn such as
"Яндекс Браузер открыт." with zero tool calls passed straight through to the
user. These cases are phrases a local model really produced in a voice session
without running any tool.
"""

from __future__ import annotations

import pytest

from jarvis.brain.action_honesty import (
    action_not_started_phrase,
    has_unbacked_action_claim,
    replace_unbacked_action_claim,
)


@pytest.mark.parametrize(
    "text",
    [
        "Яндекс Браузер открыт.",
        "Я открыл для вас приложение Steam. Оно запустилось и теперь готово к работе.",
        "Я вывел окно Steam на передний план. Теперь оно активно и готово к использованию.",
        "Я проверил погоду на Гавайях. Сейчас там солнечно, температура около 25 градусов.",
        "Проверил погоду в Мытищах, онлайн-поиск показывает, что дождя нет.",
        "Сейчас открою Яндекс Браузер.",
        "Одну секунду, я проверю погоду и скажу вам.",
    ],
)
def test_russian_unbacked_action_claims_are_detected(text: str) -> None:
    assert has_unbacked_action_claim(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Привет! Я Джарвис, чем могу помочь?",
        "Сейчас в Мытищах плюс четыре градуса, облачно.",
        "Чтобы открыть браузер, нужно выполнить команду start.",
        "Вы хотели узнать погоду в Мытищах, верно?",
        "Файл открыт в другой программе, закройте его.",
    ],
)
def test_ordinary_russian_text_is_left_alone(text: str) -> None:
    assert has_unbacked_action_claim(text) is False


def test_a_claim_is_replaced_by_an_honest_russian_fallback() -> None:
    result = replace_unbacked_action_claim(
        "Яндекс Браузер открыт.", executed_tools=set(), language="ru"
    )

    assert result == action_not_started_phrase("ru")
    assert result != action_not_started_phrase("en")


def test_a_claim_backed_by_a_real_tool_call_is_kept() -> None:
    text = "Яндекс Браузер открыт."

    assert (
        replace_unbacked_action_claim(text, executed_tools={"RunCommand"}, language="ru")
        == text
    )


def test_a_promise_in_front_of_a_real_answer_keeps_the_answer() -> None:
    result = replace_unbacked_action_claim(
        "Сейчас проверю. Погода в Мытищах плюс четыре, облачно.",
        executed_tools=set(),
        language="ru",
    )

    assert result == "Погода в Мытищах плюс четыре, облачно."
