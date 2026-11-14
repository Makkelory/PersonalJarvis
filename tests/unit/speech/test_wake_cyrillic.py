"""Cyrillic wake words: transliteration, sound-alike matching and grammar variants (fork addition)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from jarvis.plugins.wake.vosk_kws_provider import sound_confirm
from jarvis.speech.wake_aliases import grammar_variants
from jarvis.speech.wake_constants import normalize_phrase_for_match, phrase_core_for_match


@pytest.mark.parametrize(
    ("spoken", "tokens"),
    [
        ("Джарвис", ["jarvis"]),
        ("жарвис", ["jarvis"]),
        ("ярвис", ["jarvis"]),
        ("чарвис", ["jarvis"]),
        ("гарвис", ["jarvis"]),
        ("джервиз", ["jervis"]),
        ("Hey Джарвис", ["hey", "jarvis"]),
        ("хей джарвис", ["hey", "jarvis"]),
        ("эй Джарвис", ["hey", "jarvis"]),
        ("Hey Jarvis", ["hey", "jarvis"]),
        ("Computer", ["computer"]),
    ],
)
def test_phrases_normalise_into_one_phonetic_space(spoken: str, tokens: list[str]) -> None:
    assert normalize_phrase_for_match(spoken) == tokens


def test_core_of_a_prefixed_cyrillic_phrase_is_the_name() -> None:
    assert phrase_core_for_match("Hey Джарвис") == ["jarvis"]
    assert phrase_core_for_match("Джарвис") == ["jarvis"]


@pytest.mark.parametrize(
    "heard",
    [
        "джарвис", "жарвис", "джервис", "джарвес", "джарвиз", "ярвис", "джарви", "харвис", "гарвис",
        "чарвис", "джорвис", "джервиз", "ярвес", "гервис", "дзарвис", "шарвис", "джа рвис", "джар вис",
        "jarvis", "jervis", "ну джарвис",
    ],
)
def test_distorted_spellings_confirm_the_bare_wake_word(heard: str) -> None:
    assert sound_confirm(heard, "Джарвис") is True


@pytest.mark.parametrize(
    "heard",
    ["привет", "дарья", "компьютер", "алиса", "корвис", "service", "travis", "davis", "жара", "включи музыку"],
)
def test_unrelated_words_do_not_confirm(heard: str) -> None:
    assert sound_confirm(heard, "Джарвис") is False


def test_prefixed_phrase_still_needs_a_prefix() -> None:
    assert sound_confirm("хей джарвис", "Hey Джарвис") is True
    assert sound_confirm("джарвис", "Hey Джарвис") is False


@pytest.mark.parametrize("phrase", ["Hey Джарвис", "Computer", "ok google"])
def test_only_one_word_phrases_get_grammar_variants(phrase: str) -> None:
    assert grammar_variants(phrase) == ()


def test_user_alias_file_is_read_for_one_word_phrases() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "wake_aliases.json").write_text(
            json.dumps({"джарвис": ["харви", "джаред", "два слова"]}, ensure_ascii=False), encoding="utf-8"
        )
        old = os.environ.get("JARVIS__MEMORY__DATA_DIR")
        os.environ["JARVIS__MEMORY__DATA_DIR"] = tmp
        try:
            assert grammar_variants("Джарвис") == ("харви", "джаред")
        finally:
            if old is None:
                os.environ.pop("JARVIS__MEMORY__DATA_DIR", None)
            else:
                os.environ["JARVIS__MEMORY__DATA_DIR"] = old
