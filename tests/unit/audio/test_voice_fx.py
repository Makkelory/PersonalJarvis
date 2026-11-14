"""The streaming "assistant colour" for synthesized speech (fork addition)."""
from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio import voice_fx
from jarvis.audio.voice_fx import FxSettings, VoiceFx

RATE = 22050


def _speechlike(seconds: float = 1.5) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    sig = 0.3 * np.sin(2 * np.pi * 140 * t) + 0.15 * np.sin(2 * np.pi * 2800 * t)
    sig *= (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)) ** 2
    return (sig * 32767).astype(np.int16).tobytes()


def test_chunked_processing_equals_one_pass_so_chunk_joins_do_not_click() -> None:
    pcm = _speechlike()
    whole = VoiceFx(FxSettings("jarvis", 0.7)).process(pcm, RATE)
    fx = VoiceFx(FxSettings("jarvis", 0.7))
    chunked = b"".join(fx.process(pcm[i:i + 2048], RATE) for i in range(0, len(pcm), 2048))

    a = np.frombuffer(whole, dtype=np.int16).astype(np.int32)
    b = np.frombuffer(chunked, dtype=np.int16).astype(np.int32)
    assert a.shape == b.shape and int(np.abs(a - b).max()) <= 2


@pytest.mark.parametrize("strength", [0.3, 0.7, 1.0])
def test_no_clipping_and_loudness_stays_near_the_dry_voice(strength: float) -> None:
    pcm = _speechlike()
    wet = np.frombuffer(VoiceFx(FxSettings("jarvis", strength)).process(pcm, RATE), dtype=np.int16).astype(np.float32)
    dry = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)

    assert np.abs(wet).max() < 32000
    ratio = float(np.sqrt((wet ** 2).mean()) / np.sqrt((dry ** 2).mean()))
    assert 0.6 < ratio < 1.6


def test_flush_returns_a_decaying_tail_and_nothing_before_any_audio() -> None:
    assert VoiceFx(FxSettings("jarvis", 0.6)).flush() == b""
    fx = VoiceFx(FxSettings("jarvis", 0.6))
    fx.process(_speechlike(0.5), RATE)
    tail = np.frombuffer(fx.flush(), dtype=np.int16).astype(np.float32)

    assert tail.size == int(RATE * 0.32)
    assert np.abs(tail[:200]).max() > np.abs(tail[-200:]).max()


def test_settings_follow_the_config_file_and_default_to_off(tmp_path, monkeypatch) -> None:
    import jarvis.core.config as cfg

    path = tmp_path / "jarvis.toml"
    monkeypatch.setattr(cfg, "resolve_config_path", lambda: path)
    monkeypatch.setattr(voice_fx, "_cache", None)

    assert not voice_fx.current_settings().active and voice_fx.for_turn() is None  # no file
    path.write_text("[tts]\nprovider = 'piper-local'\n", encoding="utf-8")
    monkeypatch.setattr(voice_fx, "_cache", None)
    assert not voice_fx.current_settings().active  # no [tts.fx] table
    path.write_text("[tts.fx]\npreset = 'jarvis'\nstrength = 0.45\n", encoding="utf-8")
    monkeypatch.setattr(voice_fx, "_cache", None)
    settings = voice_fx.current_settings()
    assert settings.active and settings.strength == pytest.approx(0.45) and voice_fx.for_turn() is not None
    path.write_text("[tts.fx]\npreset = 'jarvis'\nstrength = 7\n", encoding="utf-8")
    monkeypatch.setattr(voice_fx, "_cache", None)
    assert voice_fx.current_settings().strength == 1.0  # clamped


def test_pitch_is_read_clamped_and_works_without_the_preset(tmp_path, monkeypatch) -> None:
    import jarvis.core.config as cfg

    path = tmp_path / "jarvis.toml"
    monkeypatch.setattr(cfg, "resolve_config_path", lambda: path)
    path.write_text("[tts.fx]\npreset = 'off'\npitch = -1.5\n", encoding="utf-8")
    monkeypatch.setattr(voice_fx, "_cache", None)
    settings = voice_fx.current_settings()
    assert settings.pitch == pytest.approx(-1.5) and not settings.active
    path.write_text("[tts.fx]\npitch = -40\n", encoding="utf-8")
    monkeypatch.setattr(voice_fx, "_cache", None)
    assert voice_fx.current_settings().pitch == -6.0
