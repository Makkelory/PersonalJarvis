"""GigaAM v3 on-device Russian recogniser (fork addition)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from jarvis.plugins.stt.gigaam_local import MODEL_FILES, GigaAMLocalSTT, default_model_dir

_MODEL_DIR = default_model_dir()
_HAVE_MODEL = all((_MODEL_DIR / name).is_file() for name in MODEL_FILES)


def test_declares_itself_as_an_on_device_recogniser() -> None:
    provider = GigaAMLocalSTT()

    assert provider.name == "gigaam-local"
    assert provider.runs_on_device is True
    assert provider.supports_streaming is False


def test_the_factory_pin_keyword_is_accepted() -> None:
    # build_stt_from_config passes ``model=`` when the user pinned one.
    GigaAMLocalSTT(language="ru", model="giga-am-v3-russian-transducer", num_threads=2)


@pytest.mark.asyncio
async def test_a_missing_model_gives_one_honest_error(tmp_path: Path) -> None:
    provider = GigaAMLocalSTT(model_dir=tmp_path)

    with pytest.raises(RuntimeError, match="GigaAM model files are not on this machine"):
        await provider.transcribe_pcm(np.ones(1600, dtype=np.int16).tobytes())


@pytest.mark.asyncio
async def test_empty_audio_is_an_empty_transcript() -> None:
    result = await GigaAMLocalSTT().transcribe_pcm(b"")

    assert result.text == ""
    assert result.confidence == 0.0


@pytest.mark.skipif(not _HAVE_MODEL, reason="GigaAM model files are not unpacked on this machine")
@pytest.mark.asyncio
async def test_transcribes_the_models_own_russian_sample() -> None:
    with wave.open(str(_MODEL_DIR / "test_wavs" / "example.wav"), "rb") as wav:
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())

    result = await GigaAMLocalSTT(language="ru").transcribe_pcm(pcm, rate, "ru")

    assert "похвал" in result.text
    assert result.language == "ru"
    assert result.confidence == 1.0
